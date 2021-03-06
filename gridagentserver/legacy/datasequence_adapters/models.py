# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import unicode_literals

from fractions import Fraction
import itertools

from django.db import models
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import MultipleObjectsReturned

from gridplatform.consumptions.models import Consumption
from gridplatform.datasequences.models import NonaccumulationDataSequence
from gridplatform.datasequences.models.accumulation import NonpulseAccumulationPeriodMixin  # noqa
from gridplatform.datasequences.models.accumulation import PulseAccumulationPeriodMixin  # noqa
from gridplatform.productions.models import Production
from gridplatform.utils import DATETIME_MAX
from gridplatform.utils import DATETIME_MIN
from gridplatform.utils import condense
from gridplatform.utils.unitconversion import PhysicalQuantity
from legacy.devices.models import PhysicalInput
from legacy.measurementpoints.models import DataSeries
from gridplatform.utils.samples import Sample
from gridplatform.utils.samples import wrap_ranged_sample_sequence

HOUR_MAX = DATETIME_MAX.replace(minute=0, second=0, microsecond=0)


def is_clock_hour(timestamp):
    return timestamp.minute == timestamp.second == timestamp.microsecond == 0


def interpolate(timestamp, point_a, point_b):
    timestamp_a, value_a = point_a.timestamp, point_a.value
    timestamp_b, value_b = point_b.timestamp, point_b.value
    assert timestamp_a <= timestamp < timestamp_b
    assert timestamp.microsecond == 0
    assert timestamp_a.microsecond == 0
    assert timestamp_b.microsecond == 0
    return value_a + (value_b - value_a) * \
        Fraction(int((timestamp - timestamp_a).total_seconds()),
                 int((timestamp_b - timestamp_a).total_seconds()))


class DataSequenceAdapterUnicodeMixin(object):
    def __unicode__(self):
        # NOTE: data sequences in general cannot be assumed to have correct
        # name.  In particular, some data sequences has been given an erronuous
        # autogenerated name.  Fortunately, it is not possible to name a data
        # sequence via legacy code.  We therefore default to the name the data
        # sequence should have been given (namely the unicode representation of
        # the corresponding physical input), if nothing suspicious is going on.
        # If more than one data source is associated with the data sequence we
        # say something suspicious is going on and the data sequence is assumed
        # to have a correct unicode representation.  At least we can't do
        # better than that for now.
        try:
            datasource_id = self.datasequence.period_set.get().\
                subclass_instance.datasource_id
            return unicode(PhysicalInput.objects.get(id=datasource_id))
        except (MultipleObjectsReturned, ObjectDoesNotExist):
            return unicode(self.datasequence)


class AccumulationAdapterBase(DataSequenceAdapterUnicodeMixin, DataSeries):
    # NOTE: Need different classes to wrap consumption and production...

    class Meta:
        abstract = True

    def _assert_invariants(self):
        assert self.unit == self.datasequence.unit, \
            '{} != {}'.format(self.unit, self.datasequence.unit)

    def get_recursive_condense_resolution(self, resolution):
        if condense.RESOLUTIONS.index(resolution) >= \
                condense.RESOLUTIONS.index(condense.DAYS):
            return None
        else:
            return condense.next_resolution(resolution)

    def _get_samples(self, from_timestamp, to_timestamp):
        ZERO = PhysicalQuantity(0, self.unit)
        period_offset = ZERO

        def add_period_offset(sample):
            """
            Add period offset from given C{sample} to make samples of two
            successive periods appear continuous.
            """
            return sample._replace(
                physical_quantity=sample.physical_quantity + period_offset)

        input_periods = iter(
            wrap_period(period.subclass_instance)
            for period in self.datasequence.period_set.in_range(
                from_timestamp, to_timestamp).order_by('from_timestamp'))

        last_sample_yielded = None
        try:
            first_input_period = next(input_periods)
        except StopIteration:
            # no input periods
            pass
        else:
            # yield extrapolated sample before first period if necessary
            if first_input_period.from_timestamp > from_timestamp:
                first_sample = self.create_point_sample(
                    from_timestamp,
                    PhysicalQuantity(0, self.unit),
                    uncachable=True,
                    extrapolated=True)
                yield first_sample
                last_sample_yielded = first_sample

            # yield samples of first period
            next_period_offset = ZERO
            for sample in first_input_period.get_samples(
                    max(from_timestamp, first_input_period.from_timestamp),
                    min(to_timestamp, first_input_period.to_timestamp)):
                yield sample
                period_offset = sample.physical_quantity
                last_sample_yielded = sample

            # yield samples of remaining periods with added offset from
            # previous period, and skipping first sample of each period as it
            # would otherwise be a duplicate of the last sample of the previous
            # period.
            for input_period in input_periods:
                input_period = input_period
                next_period_offset = ZERO
                first_sample_in_period = True
                for sample in input_period.get_samples(
                        max(from_timestamp, input_period.from_timestamp),
                        min(to_timestamp, input_period.to_timestamp)):
                    if not first_sample_in_period:
                        yield add_period_offset(sample)
                        last_sample_yielded = sample
                    next_period_offset = sample.physical_quantity
                    first_sample_in_period = False
                period_offset = next_period_offset

            # yield extrapolated sample after last period if necessary
            if last_sample_yielded is not None and \
                    last_sample_yielded.timestamp != to_timestamp:
                yield last_sample_yielded._replace(
                    timestamp=to_timestamp,
                    cachable=False,
                    extrapolated=True)
        if last_sample_yielded is None:
            yield self.create_point_sample(
                from_timestamp,
                PhysicalQuantity(0, self.unit),
                uncachable=True,
                extrapolated=True)
            yield self.create_point_sample(
                to_timestamp,
                PhysicalQuantity(0, self.unit),
                uncachable=True,
                extrapolated=True)

    def _get_condensed_samples(
            self, from_timestamp, sample_resolution, to_timestamp):
        return wrap_ranged_sample_sequence(
            self.datasequence.development_sequence(
                from_timestamp, to_timestamp, sample_resolution))

    def calculate_development(self, from_timestamp, to_timestamp):
        # The "normal"/inherited DataSeries calculate_development() is based on
        # iterating over get_samples() --- usually far from optimal, but
        # reduces the amount of code necessary to implement the DataSeries
        # interface.  If the from/to timestamps match clock hours, we may
        # optimise by using the development_sum()-method on the datasequence
        # object --- other optimisations would take more effort for less gain.
        # (Total consumption over a period with calculate_development() is
        # usually used in a context where the period is days or months and thus
        # implicitly match clock hours; the overhead from the inherited
        # calculate_development() is not all that significant for periods
        # shorter than hours...)
        if is_clock_hour(from_timestamp) and is_clock_hour(to_timestamp):
            value = self.datasequence.development_sum(
                from_timestamp, to_timestamp)
            return Sample(from_timestamp, to_timestamp, value, False, False)
        else:
            return super(
                AccumulationAdapterBase, self).calculate_development(
                from_timestamp, to_timestamp)


class ConsumptionAccumulationAdapter(AccumulationAdapterBase):
    datasequence = models.ForeignKey(
        Consumption, on_delete=models.PROTECT, related_name='+')


class ProductionAccumulationAdapter(AccumulationAdapterBase):
    datasequence = models.ForeignKey(
        Production, on_delete=models.PROTECT, related_name='+')


class PeriodAdapterBase(object):
    def __init__(self, period):
        self.period = period

    @property
    def datasource(self):
        # ... might not exist...
        return self.period.datasource

    @property
    def unit(self):
        return self.period.unit

    @cached_property
    def from_timestamp(self):
        if self.period.from_timestamp is not None:
            return self.period.from_timestamp
        else:
            return DATETIME_MIN

    @cached_property
    def to_timestamp(self):
        if self.period.to_timestamp is not None:
            return self.period.to_timestamp
        else:
            return HOUR_MAX

    def _get_leading_raw_data(self, timestamp):
        return self.period.datasource.rawdata_set.filter(
            timestamp__lt=timestamp).order_by('timestamp').last()

    @cached_property
    def _leading_raw_data(self):
        return self._get_leading_raw_data(self.from_timestamp)

    def _get_following_raw_data(self, timestamp):
        return self.period.datasource.rawdata_set.filter(
            timestamp__gt=timestamp).order_by('timestamp').first()

    @cached_property
    def _first_raw_data(self):
        return self.period.datasource.rawdata_set.filter(
            timestamp__gte=self.from_timestamp,
            timestamp__lte=self.to_timestamp).order_by('timestamp').first()

    def create_point_sample(
            self, timestamp, physical_quantity,
            cachable=True, extrapolated=False):
        return Sample(
            timestamp,
            timestamp,
            physical_quantity,
            cachable,
            extrapolated)

    def _interpolate_sample(self, timestamp, raw_data_before, raw_data_after):
        return self.create_point_sample(
            timestamp,
            PhysicalQuantity(
                interpolate(
                    timestamp,
                    raw_data_before,
                    raw_data_after),
                self.unit))

    def get_samples(self, from_timestamp, to_timestamp):
        assert from_timestamp <= to_timestamp
        assert from_timestamp >= self.from_timestamp
        assert to_timestamp <= self.to_timestamp
        return self._get_samples(from_timestamp, to_timestamp)


class AccumulationPeriodAdapterBase(PeriodAdapterBase):
    def get_samples(self, from_timestamp, to_timestamp):
        previous_sample_yielded = None
        samples = iter(
            super(AccumulationPeriodAdapterBase, self).get_samples(
                from_timestamp, to_timestamp))
        try:
            first_sample = next(samples)
        except StopIteration:
            # no samples
            return
        assert first_sample.timestamp == from_timestamp
        # check postconditions of first sample yielded
        if from_timestamp == self.from_timestamp:
            assert not first_sample.physical_quantity
        yield first_sample
        previous_sample_yielded = first_sample
        for sample in samples:
            yield sample
            previous_sample_yielded = sample
        final_sample_yielded = previous_sample_yielded
        # check postconditions of final sample yielded
        assert final_sample_yielded.timestamp == to_timestamp

    @cached_property
    def offset(self):
        if self._first_raw_data is None:
            if self._leading_raw_data is None:
                return PhysicalQuantity(0, self.unit)
            else:
                return PhysicalQuantity(
                    self._leading_raw_data.value, self.unit)
        else:
            assert self._first_raw_data is not None
            if self._first_raw_data.timestamp == self.from_timestamp:
                return PhysicalQuantity(
                    self._first_raw_data.value,
                    self.unit)
            elif self._leading_raw_data is not None:
                return PhysicalQuantity(
                    interpolate(
                        self.from_timestamp,
                        self._leading_raw_data,
                        self._first_raw_data),
                    self.unit)
            else:
                return PhysicalQuantity(self._first_raw_data.value, self.unit)

    def _extrapolate_sample(self, timestamp, raw_data):
        return self.create_point_sample(
            timestamp,
            PhysicalQuantity(raw_data.value, self.unit),
            cachable=False, extrapolated=True)

    def _subtract_offset(self, sample):
        return sample._replace(
            physical_quantity=sample.physical_quantity - self.offset)

    def _get_samples(self, from_timestamp, to_timestamp):
        return self._get_converted_samples(
            from_timestamp, to_timestamp)

    def _get_raw_samples(self, from_timestamp, to_timestamp):
        assert from_timestamp <= to_timestamp
        assert self.from_timestamp <= from_timestamp
        assert self.to_timestamp >= to_timestamp
        raw_data_iterator = iter(
            self.datasource.rawdata_set.filter(
                timestamp__gte=from_timestamp, timestamp__lte=to_timestamp))
        try:
            first_raw_data = next(raw_data_iterator)
        except StopIteration:
            leading_raw_data = self._get_leading_raw_data(from_timestamp)
            following_raw_data = self._get_following_raw_data(to_timestamp)
            if leading_raw_data is not None and following_raw_data is not None:
                if from_timestamp == to_timestamp:
                    # single sample interpolated
                    yield self._subtract_offset(
                        self._interpolate_sample(
                            from_timestamp, leading_raw_data,
                            following_raw_data))
                else:
                    # two samples interpolated
                    yield self._subtract_offset(
                        self._interpolate_sample(
                            from_timestamp, leading_raw_data,
                            following_raw_data))
                    yield self._subtract_offset(
                        self._interpolate_sample(
                            to_timestamp, leading_raw_data,
                            following_raw_data))
            elif leading_raw_data is not None or \
                    following_raw_data is not None:
                raw_data = leading_raw_data or following_raw_data
                if from_timestamp == to_timestamp:
                    # single sample extrapolated
                    yield self._subtract_offset(
                        self._extrapolate_sample(from_timestamp, raw_data))
                else:
                    # two samples extrapolated
                    yield self._subtract_offset(
                        self._extrapolate_sample(from_timestamp, raw_data))
                    yield self._subtract_offset(
                        self._extrapolate_sample(to_timestamp, raw_data))
            return
        # yield sample before first raw data if missing
        if first_raw_data.timestamp != from_timestamp:
            leading_raw_data = self._get_leading_raw_data(
                max(self.from_timestamp, from_timestamp))
            if leading_raw_data:
                first_sample = self._interpolate_sample(
                    from_timestamp, leading_raw_data, first_raw_data)
            else:
                first_sample = self._extrapolate_sample(
                    from_timestamp, first_raw_data)
            yield self._subtract_offset(first_sample)
        # yield the sample of the first raw data
        yield self._subtract_offset(
            self.create_point_sample(
                first_raw_data.timestamp,
                PhysicalQuantity(first_raw_data.value, self.unit)))
        final_raw_data = first_raw_data
        # yield samples for remaining raw data
        for raw_data in raw_data_iterator:
            yield self._subtract_offset(
                self.create_point_sample(
                    raw_data.timestamp,
                    PhysicalQuantity(raw_data.value, self.unit)))
            final_raw_data = raw_data
        # yield final sample after final raw data if missing
        if final_raw_data.timestamp != to_timestamp:
            following_raw_data = self._get_following_raw_data(
                min(self.to_timestamp, to_timestamp))
            if following_raw_data:
                final_sample = self._interpolate_sample(
                    to_timestamp, final_raw_data, following_raw_data)
            else:
                final_sample = self._extrapolate_sample(
                    to_timestamp, final_raw_data)
            yield self._subtract_offset(final_sample)


class PulseAccumulationPeriodAdapter(AccumulationPeriodAdapterBase):
    @cached_property
    def _conversion_factor(self):
        pulse_quantity = PhysicalQuantity(
            self.period.pulse_quantity, 'impulse')
        output_quantity = PhysicalQuantity(
            self.period.output_quantity, self.period.output_unit)
        return output_quantity / pulse_quantity

    def _convert_sample(self, sample):
        return sample._replace(
            physical_quantity=self._conversion_factor *
            sample.physical_quantity)

    def _get_converted_samples(self, from_timestamp, to_timestamp):
        return itertools.imap(
            self._convert_sample,
            self._get_raw_samples(
                from_timestamp, to_timestamp))


class NonpulseAccumulationPeriodAdapter(AccumulationPeriodAdapterBase):
    def _get_converted_samples(self, from_timestamp, to_timestamp):
        return self._get_raw_samples(from_timestamp, to_timestamp)


class UnsupportedPeriodAdapter(AccumulationPeriodAdapterBase):
    # SingleValueAccumulationPeriod, ConversionAccumulationPeriod: Avoid
    # crashing --- but make no further promises...
    def get_samples(self, from_timestamp, to_timestamp):
        return []


def wrap_period(period):
    if isinstance(period, NonpulseAccumulationPeriodMixin):
        return NonpulseAccumulationPeriodAdapter(period)
    elif isinstance(period, PulseAccumulationPeriodMixin):
        return PulseAccumulationPeriodAdapter(period)
    else:
        return UnsupportedPeriodAdapter(period)


class NonaccumulationPeriodAdapter(PeriodAdapterBase):
    def _get_samples(self, from_timestamp, to_timestamp):
        return self._get_samples_pure_implementation(
            from_timestamp, to_timestamp)

    def _get_samples_pure_implementation(self, from_timestamp, to_timestamp):
        assert hasattr(self, 'datasource')
        assert from_timestamp <= to_timestamp

        raw_data_iterator = iter(
            self.datasource.rawdata_set.filter(
                timestamp__gte=max(from_timestamp, self.from_timestamp),
                timestamp__lte=min(to_timestamp, self.to_timestamp)))

        try:
            first_raw_data = next(raw_data_iterator)
        except StopIteration:
            leading_raw_data = self._get_leading_raw_data(from_timestamp)
            following_raw_data = self._get_following_raw_data(to_timestamp)
            if leading_raw_data is not None and following_raw_data is not None:
                if from_timestamp == to_timestamp:
                    # single sample interpolated
                    yield self._interpolate_sample(
                        from_timestamp, leading_raw_data, following_raw_data)
                else:
                    # two samples interpolated
                    yield self._interpolate_sample(
                        from_timestamp, leading_raw_data, following_raw_data)
                    yield self._interpolate_sample(
                        to_timestamp, leading_raw_data, following_raw_data)
            return

        # yield sample before first raw data if missing
        if first_raw_data.timestamp != from_timestamp:
            leading_raw_data = self._get_leading_raw_data(from_timestamp)
            if leading_raw_data:
                yield self._interpolate_sample(
                    from_timestamp, leading_raw_data, first_raw_data)

        # yield the sample of the first raw data
        yield self.create_point_sample(
            first_raw_data.timestamp,
            PhysicalQuantity(first_raw_data.value, self.unit))
        final_raw_data = first_raw_data

        # yield samples for remaining raw data
        for raw_data in raw_data_iterator:
            yield self.create_point_sample(
                raw_data.timestamp,
                PhysicalQuantity(raw_data.value, self.unit))
            final_raw_data = raw_data

        # yield final sample after final raw data if missing
        if final_raw_data.timestamp != to_timestamp:
            following_raw_data = self._get_following_raw_data(to_timestamp)
            if following_raw_data:
                yield self._interpolate_sample(
                    to_timestamp, final_raw_data, following_raw_data)


class NonaccumulationAdapter(DataSequenceAdapterUnicodeMixin, DataSeries):
    datasequence = models.ForeignKey(
        NonaccumulationDataSequence,
        on_delete=models.PROTECT, related_name='+')

    class Meta:
        verbose_name = _('nonaccumulation adapter')
        verbose_name_plural = _('nonaccumulation adapters')

    def get_recursive_condense_resolution(self, resolution):
        if condense.RESOLUTIONS.index(resolution) >= \
                condense.RESOLUTIONS.index(condense.DAYS):
            return None
        else:
            return condense.next_resolution(resolution)

    def _get_samples(self, from_timestamp, to_timestamp):
        input_periods = iter(
            NonaccumulationPeriodAdapter(period)
            for period in self.datasequence.period_set.in_range(
                from_timestamp, to_timestamp).order_by('from_timestamp'))

        for input_period in input_periods:
            for sample in input_period.get_samples(
                    max(from_timestamp, input_period.from_timestamp),
                    min(to_timestamp, input_period.to_timestamp)):
                # Adhere to postcondition defined by DataSeries.get_samples().
                if sample.timestamp != input_period.to_timestamp or \
                        input_period.to_timestamp == to_timestamp:
                    yield sample
