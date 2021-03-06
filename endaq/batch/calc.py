from functools import partial
import logging
import os

import numpy as np
import pandas as pd
import idelib

from endaq.batch.analyzer import Analyzer
from endaq.batch.utils.calc import stats as utils_stats
from endaq.batch.utils.calc import psd as utils_psd


def _make_meta(dataset):
    """Generate a pandas object containing metadata for the given recording."""
    serial_no = dataset.recorderInfo["RecorderSerial"]
    start_time = np.datetime64(dataset.sessions[0].utcStartTime, "s") + np.timedelta64(
        dataset.sessions[0].firstTime, "us"
    )

    return pd.Series(
        [serial_no, start_time],
        index=["serial number", "start time"],
        name=dataset.filename,
    )


def _make_psd(analyzer, fstart=None, bins_per_octave=None):
    """
    Format the PSD of the main accelerometer channel into a pandas object.

    The PSD is scaled to units of g^2/Hz (g := gravity = 9.80665 meters per
    square second).
    """
    accel_ch = analyzer._channels.get("acc", None)
    if accel_ch is None:
        return None

    f, psd = analyzer._PSDData
    if bins_per_octave is not None:
        f, psd = utils_psd.to_octave(
            f,
            psd,
            fstart=(fstart or 1),
            octave_bins=bins_per_octave,
            axis=1,
            mode="mean",
        )

    df_psd = pd.DataFrame(
        psd.T * analyzer.MPS2_TO_G ** 2,  # (m/s^2)^2/Hz -> g^2/Hz
        index=pd.Index(f, name="frequency"),
        columns=pd.Index(accel_ch.axis_names, name="axis"),
    )

    df_psd["Resultant"] = np.sum(df_psd.to_numpy(), axis=1)

    return df_psd.stack(level="axis").reorder_levels(["axis", "frequency"])


def _make_pvss(analyzer):
    """
    Format the PVSS of the main accelerometer channel into a pandas object.

    The PVSS is scaled to units of mm/sec.
    """
    accel_ch = analyzer._channels.get("acc", None)
    if accel_ch is None:
        return None

    f, pvss = analyzer._PVSSData

    df_pvss = pd.DataFrame(
        pvss.T * analyzer.MPS_TO_MMPS,
        index=pd.Index(f, name="frequency"),
        columns=pd.Index(accel_ch.axis_names, name="axis"),
    )

    df_pvss["Resultant"] = utils_stats.L2_norm(df_pvss.to_numpy(), axis=1)

    return df_pvss.stack(level="axis").reorder_levels(["axis", "frequency"])


def _make_metrics(analyzer):
    """
    Format the channel metrics of a recording into a pandas object.

    The following units listed by type are used for the metrics:
    - acceleration - g (gravity = 9.80665 meters per square second)
    - velocity - millimeters per second
    - displacement - millimeters
    - rotation speed - degrees per second
    - GPS position - degrees latitude/longitude
    - GPS speed - km/h (kilometers per hour)
    - audio - unitless
    - temperature - degrees Celsius
    - pressure - kiloPascals
    """
    df = pd.DataFrame(
        [
            analyzer.accRMSFull,
            analyzer.velRMSFull,
            analyzer.disRMSFull,
            analyzer.accPeakFull,
            analyzer.pseudoVelPeakFull,
            analyzer.gpsLocFull,
            analyzer.gpsSpeedFull,
            analyzer.gyroRMSFull,
            analyzer.micRMSFull,
            analyzer.tempFull,
            analyzer.pressFull,
        ]
    )

    # Format data into desired shape
    df.index.name = "calculation"
    series = df.stack()

    return series


def _make_peak_windows(analyzer, margin_len):
    """
    Store windows of the main accelerometer channel about its peaks in a pandas
    object.

    The acceleration is scaled to units of g (gravity = 9.80665 meters per
    square second).
    """
    accel_ch = analyzer._channels.get("acc", None)
    if accel_ch is None:
        return None

    data = analyzer.MPS2_TO_G * np.concatenate(  # m/s^2 -> g
        [analyzer._accelerationData, analyzer._accelerationResultant[None]], axis=0
    )

    window_len = 2 * margin_len + 1
    t = (
        accel_ch.eventarray.arraySlice()[0]
        - accel_ch.channel.dataset.sessions[0].firstTime
    )
    dt = 1 / analyzer._accelerationFs
    result_data = np.full((window_len, data.shape[0]), np.nan, dtype=data.dtype)

    # Calculate ranges
    i_max = np.argmax(np.abs(data), axis=1)
    i_max_neg = i_max - data.shape[1]
    for j in range(data.shape[0]):
        result_data[-margin_len - 1 - i_max[j] : margin_len - i_max_neg[j], j] = data[
            j, i_max_neg[j] - margin_len : i_max[j] + margin_len + 1
        ]

    # Format results
    result = (
        pd.DataFrame(
            result_data,
            index=pd.to_timedelta(
                dt * pd.RangeIndex(-margin_len, margin_len + 1, name="peak offset"),
                unit="s",
            ),
            columns=pd.MultiIndex.from_arrays(
                [
                    accel_ch.axis_names + ["Resultant"],
                    t[i_max].astype("timedelta64[us]"),
                ],
                names=["axis", "peak time"],
            ),
        )
        .stack(level="axis")
        .stack(level="peak time")
        .reorder_levels(["axis", "peak time", "peak offset"])
    )

    return result


def _make_vc_curves(analyzer):
    """
    Format the VC curves of the main accelerometer channel into a pandas object.
    """

    accel_ch = analyzer._channels.get("acc", None)
    if accel_ch is None:
        return None

    f, vc = analyzer._VCCurveData

    df_vc = pd.DataFrame(
        vc.T * analyzer.MPS_TO_UMPS,  # (m/s) -> (??m/s)
        index=pd.Index(f, name="frequency"),
        columns=pd.Index(accel_ch.axis_names, name="axis"),
    )

    df_vc["Resultant"] = utils_stats.L2_norm(df_vc.to_numpy(), axis=1)

    return df_vc.stack(level="axis").reorder_levels(["axis", "frequency"])


class GetDataBuilder:
    """
    The main interface for the calculations.

    This object has two types of functions:
    - configuration functions - these determine what calculations will be
      performed on IDE recordings, and pass in any requisite parameters for said
      calculations.

      This includes the following functions:
      - add_psd
      - add_pvss
      - add_metrics
      - add_peaks
      - add_vc_curves
    - execution functions - these functions take recording files as parameters,
      perform the configured calculations on the data therein, and return the
      calculated data as pandas objects.

      This includes the functions `_get_data` & `aggregate_data`, which operates
      on one & multiple file(s), respectively.

    A typical use case will look something like this:

    ```python
    filenames = [...]

    calc_output = (
        GetDataBuilder(accel_highpass_cutoff=1)
        .add_psd(freq_bin_width=1)
        .add_pvss(init_freq=1, bins_per_octave=12)
        .add_metrics()
        .add_peaks(margin_len=100)
        .add_vc_curves(init_freq=1, bins_per_octave=3)
        .aggregate_data(filenames)
    )
    file_data = calc_output.dataframes
    ```

    """

    def __init__(
        self,
        *,
        preferred_chs=[],
        accel_highpass_cutoff,
        accel_start_time=None,
        accel_end_time=None,
        accel_start_margin=None,
        accel_end_margin=None,
    ):
        """
        Constructor.

        :param preferred_chs: a sequence of channels; each gets priority over
            others of its unit type
        :param accel_highpass_cutoff: the cutoff frequency used when
            pre-filtering acceleration data
        """
        if accel_start_time is not None and accel_start_margin is not None:
            raise ValueError(
                "only one of `accel_start_time` and `accel_start_margin` may be set at once"
            )
        if accel_end_time is not None and accel_end_margin is not None:
            raise ValueError(
                "only one of `accel_end_time` and `accel_end_margin` may be set at once"
            )

        self._metrics_queue = {}  # dict maintains insertion order, unlike set

        self._analyzer_kwargs = dict(
            preferred_chs=preferred_chs,
            accel_highpass_cutoff=accel_highpass_cutoff,
            accel_start_time=accel_start_time,
            accel_end_time=accel_end_time,
            accel_start_margin=accel_start_margin,
            accel_end_margin=accel_end_margin,
        )

        # Even unused parameters MUST be set; used to instantiate `Analyzer` in `_get_data`
        self._psd_freq_bin_width = None
        self._psd_freq_start_octave = None
        self._psd_bins_per_octave = None
        self._psd_window = None
        self._pvss_init_freq = None
        self._pvss_bins_per_octave = None
        self._peak_window_margin_len = None
        self._vc_init_freq = None
        self._vc_bins_per_octave = None

    def add_psd(
        self,
        *,
        freq_bin_width=None,
        freq_start_octave=None,
        bins_per_octave=None,
        window="hanning",
    ):
        """
        Add the acceleration PSD to the calculation queue.

        :param freq_bin_width: the desired spacing between adjacent PSD samples;
            a default is provided only if `bins_per_octave` is used, otherwise
            this parameter is required
        :param freq_start_octave: the first frequency to use in octave-spacing;
            this is only used if `bins_per_octave` is set
        :param bins_per_octave: the number of frequency bins per octave in a
            log-spaced PSD; if not set, the PSD will be linearly-spaced as
            specified by `freq_bin_width`
        :param window: the window type used in the PSD calculation; see the
            documentation for `scipy.signal.welch` for details
        """
        if all(i is None for i in (freq_bin_width, bins_per_octave)):
            raise ValueError(
                "must at least provide parameters for one of linear and log-spaced modes"
            )
        if freq_bin_width is None:
            if freq_start_octave is None:
                freq_start_octave = 1

            fstart_breadth = 2 ** (1 / (2 * bins_per_octave)) - 2 ** (
                -1 / (2 * bins_per_octave)
            )
            freq_bin_width = freq_start_octave / int(5 / fstart_breadth)

        self._metrics_queue["psd"] = None
        self._psd_freq_bin_width = freq_bin_width
        self._psd_freq_start_octave = freq_start_octave
        self._psd_bins_per_octave = bins_per_octave
        self._psd_window = window

        return self

    def add_pvss(self, *, init_freq, bins_per_octave):
        """
        Add the acceleration PVSS (Pseudo Velocity Shock Spectrum) to the
        calculation queue.

        :param init_freq: the first frequency sample in the spectrum
        :param bins_per_octave: the number of samples per frequency octave
        """
        self._metrics_queue["pvss"] = None
        self._pvss_init_freq = init_freq
        self._pvss_bins_per_octave = bins_per_octave

        return self

    def add_metrics(self):
        """Add broad channel metrics to the calculation queue."""
        self._metrics_queue["metrics"] = None

        if "pvss" not in self._metrics_queue:
            self._pvss_init_freq = 1
            self._pvss_bins_per_octave = 12

        return self

    def add_peaks(self, *, margin_len):
        """
        Add windows about the acceleration's peak value to the calculation
        queue.

        :param margin_len: the number of samples on each side of a peak to
            include in the windows
        """
        self._metrics_queue["peaks"] = None
        self._peak_window_margin_len = margin_len

        return self

    def add_vc_curves(self, init_freq, bins_per_octave):
        """
        Add Vibration Criteria (VC) Curves to the calculation queue.

        :param init_freq: the first frequency
        :param bins_per_octave:  the number of samples per frequency octave
        """
        self._metrics_queue["vc_curves"] = None

        if "psd" not in self._metrics_queue:
            self._psd_freq_bin_width = 0.2
            self._psd_window = "hanning"
        self._vc_init_freq = init_freq
        self._vc_bins_per_octave = bins_per_octave

        return self

    def _get_data(self, filename):
        """
        Calculate data from a single recording into a pandas object.

        Used internally by `aggregate_data`.
        """
        print(f"processing {filename}...")

        data = {}
        with idelib.importFile(filename) as ds:
            analyzer = Analyzer(
                ds,
                **self._analyzer_kwargs,
                psd_window=self._psd_window,
                psd_freq_bin_width=self._psd_freq_bin_width,
                pvss_init_freq=self._pvss_init_freq,
                pvss_bins_per_octave=self._pvss_bins_per_octave,
                vc_init_freq=self._vc_init_freq,
                vc_bins_per_octave=self._vc_bins_per_octave,
            )

            data["meta"] = _make_meta(ds)

            funcs = dict(
                psd=partial(
                    _make_psd,
                    fstart=self._psd_freq_start_octave,
                    bins_per_octave=self._psd_bins_per_octave,
                ),
                pvss=_make_pvss,
                metrics=_make_metrics,
                peaks=partial(
                    _make_peak_windows,
                    margin_len=self._peak_window_margin_len,
                ),
                vc_curves=_make_vc_curves,
            )
            for output_type in self._metrics_queue.keys():
                data[output_type] = funcs[output_type](analyzer)

        return data

    def aggregate_data(self, filenames):
        """Compile configured data from the given files into a dataframe."""
        series_lists = zip(
            *(self._get_data(filename).values() for filename in filenames)
        )

        print("aggregating data...")
        meta, *dfs = (
            pd.concat(
                series_list,
                keys=filenames,
                names=["filename"]
                + next(s for s in series_list if s is not None).index.names,
            )
            if series_list and any(s is not None for s in series_list)
            else None
            for series_list in series_lists
        )

        meta = meta.unstack(level=1)

        def reformat(series):
            if series is None:
                return None

            df = series.to_frame().T.melt()
            df["serial number"] = meta.loc[df["filename"], "serial number"].reset_index(
                drop=True
            )
            df["start time"] = meta.loc[df["filename"], "start time"].reset_index(
                drop=True
            )

            return df

        dfs = dict(
            meta=meta,
            **{key: reformat(df) for (key, df) in zip(self._metrics_queue.keys(), dfs)},
        )

        print("done!")

        return OutputStruct(dfs)


class OutputStruct:
    """A data wrapper class with methods for common export operations."""

    def __init__(self, data):
        self.dataframes = data

    def to_csv_folder(self, folder_path):
        """
        Write data to a folder as CSV's.

        :param folder_path: the output directory path for .CSV files
        """
        os.makedirs(folder_path, exist_ok=True)

        for k, df in self.dataframes.items():
            path = os.path.join(folder_path, f"{k}.csv")
            df.to_csv(path, index=(k == "meta"))

    def to_html_plots(self, folder_path=None, show=False):
        """
        Generate plots in HTML.

        :param folder_path: the output directory for saving .HTML
            plots. If `None`, plots are not saved. Defaults to `None`.
        :param show: whether to open plots after generation. Defaults to `False`.
        """
        if not any((folder_path, show)):
            return

        import plotly.express as px

        if folder_path:
            os.makedirs(folder_path, exist_ok=True)

        for k, df in self.dataframes.items():
            if k == "meta":
                continue
            if k == "psd":
                fig = px.line(
                    df, x="frequency", y="value", color="filename", line_dash="axis"
                )

                fig.update_xaxes(type="log", title_text="frequency (Hz)")
                fig.update_yaxes(type="log", title_text="Acceleration (g^2/Hz)")
                fig.update_layout(title="Acceleration PSD")

            elif k == "pvss":
                fig = px.line(
                    df, x="frequency", y="value", color="filename", line_dash="axis"
                )
                fig.update_xaxes(type="log", title_text="frequency (Hz)")
                fig.update_yaxes(type="log", title_text="Velocity (mm/s)")
                fig.update_layout(title="Pseudo Velocity Shock Spectrum (PVSS)")

            elif k == "metrics":
                logging.warning("HTML plot for metrics not currently implemented")
                continue

            elif k == "peaks":
                fig = px.line(
                    df,
                    x=df["peak offset"].dt.total_seconds(),
                    # ^ plotly doesn't handle timedelta's well
                    y="value",
                    color="filename",
                    line_dash="axis",
                )
                fig.update_xaxes(title_text="time relative to peak (s)")
                fig.update_yaxes(title_text="Acceleration (g)")
                fig.update_layout(title="Window about Acceleration Peaks")

            elif k == "vc_curves":
                fig = px.line(
                    df, x="frequency", y="value", color="filename", line_dash="axis"
                )

                fig.update_xaxes(type="log", title_text="frequency (Hz)")
                fig.update_yaxes(
                    type="log", title_text="1/3-Octave RMS Velocity (??m/s)"
                )
                fig.update_layout(title="Vibration Criteria (VC) Curves")

            else:
                raise RuntimeError(f"no configuration for plotting '{k}' data")

            if not folder_path and show:
                fig.show()
            else:
                fig.write_html(
                    file=os.path.join(folder_path, f"{k}.html"),
                    include_plotlyjs="directory",
                    full_html=True,
                    auto_open=show,
                )
