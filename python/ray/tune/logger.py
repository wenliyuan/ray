from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import csv
import json
import logging
import numpy as np
import os
import yaml
import distutils.version

import ray.cloudpickle as cloudpickle
from ray.tune.log_sync import get_syncer
from ray.cloudpickle import cloudpickle
from ray.tune.result import NODE_IP, TRAINING_ITERATION, TIME_TOTAL_S, \
    TIMESTEPS_TOTAL

logger = logging.getLogger(__name__)

try:
    import tensorflow as tf
    use_tf150_api = (distutils.version.LooseVersion(tf.VERSION) >=
                     distutils.version.LooseVersion("1.5.0"))
except ImportError:
    tf = None
    use_tf150_api = True
    logger.warning("Couldn't import TensorFlow - "
                   "disabling TensorBoard logging.")


class Logger(object):
    """Logging interface for ray.tune.

    By default, the UnifiedLogger implementation is used which logs results in
    multiple formats (TensorBoard, rllab/viskit, plain json, custom loggers)
    at once.

    Arguments:
        config: Configuration passed to all logger creators.
        logdir: Directory for all logger creators to log to.
        upload_uri (str): Optional URI where the logdir is sync'ed to.
    """

    def __init__(self, config, logdir, upload_uri=None):
        self.config = config
        self.logdir = logdir
        self.uri = upload_uri
        self._init()

    def _init(self):
        pass

    def on_result(self, result):
        """Given a result, appends it to the existing log."""

        raise NotImplementedError

    def close(self):
        """Releases all resources used by this logger."""

        pass

    def flush(self):
        """Flushes all disk writes to storage."""

        pass


class UnifiedLogger(Logger):
    """Unified result logger for TensorBoard, rllab/viskit, plain json.

    This class also periodically syncs output to the given upload uri.

    Arguments:
        config: Configuration passed to all logger creators.
        logdir: Directory for all logger creators to log to.
        upload_uri (str): Optional URI where the logdir is sync'ed to.
        custom_loggers (list): List of custom logger creators.
        sync_function (func|str): Optional function for syncer to run.
            See ray/python/ray/tune/log_sync.py
    """

    def __init__(self,
                 config,
                 logdir,
                 upload_uri=None,
                 custom_loggers=None,
                 sync_function=None):
        self._logger_list = [_JsonLogger, _TFLogger, _VisKitLogger]
        self._sync_function = sync_function
        if custom_loggers:
            assert isinstance(custom_loggers, list), "Improper custom loggers."
            self._logger_list += custom_loggers

        Logger.__init__(self, config, logdir, upload_uri)

    def _init(self):
        self._loggers = []
        for cls in self._logger_list:
            try:
                self._loggers.append(cls(self.config, self.logdir, self.uri))
            except Exception:
                logger.exception("Could not instantiate {} - skipping.".format(
                    str(cls)))
        self._log_syncer = get_syncer(
            self.logdir, self.uri, sync_function=self._sync_function)

    def on_result(self, result):
        for logger in self._loggers:
            logger.on_result(result)
        self._log_syncer.set_worker_ip(result.get(NODE_IP))
        self._log_syncer.sync_if_needed()

    def close(self):
        for logger in self._loggers:
            logger.close()
        self._log_syncer.sync_now(force=True)

    def flush(self):
        for logger in self._loggers:
            logger.flush()
        self._log_syncer.sync_now(force=True)
        self._log_syncer.wait()


class NoopLogger(Logger):
    def on_result(self, result):
        pass


class _JsonLogger(Logger):
    def _init(self):
        config_out = os.path.join(self.logdir, "params.json")
        with open(config_out, "w") as f:
            json.dump(
                self.config,
                f,
                indent=2,
                sort_keys=True,
                cls=_SafeFallbackEncoder)
        pkl_out = os.path.join(self.logdir, "params.pkl")
        with open(config_out, "wb") as f:
            cloudpickle.dump(
                        self.config,
                        f)
        local_file = os.path.join(self.logdir, "result.json")
        self.local_out = open(local_file, "w")

    def on_result(self, result):
        json.dump(result, self, cls=_SafeFallbackEncoder)
        self.write("\n")

    def write(self, b):
        self.local_out.write(b)
        self.local_out.flush()

    def close(self):
        self.local_out.close()


def to_tf_values(result, path):
    values = []
    for attr, value in result.items():
        if value is not None:
            if use_tf150_api:
                type_list = [int, float, np.float32, np.float64, np.int32]
            else:
                type_list = [int, float]
            if type(value) in type_list:
                values.append(
                    tf.Summary.Value(
                        tag="/".join(path + [attr]), simple_value=value))
            elif type(value) is dict:
                values.extend(to_tf_values(value, path + [attr]))
    return values


class _TFLogger(Logger):
    def _init(self):
        self._file_writer = tf.summary.FileWriter(self.logdir)

    def on_result(self, result):
        tmp = result.copy()
        for k in [
                "config", "pid", "timestamp", TIME_TOTAL_S, TRAINING_ITERATION
        ]:
            del tmp[k]  # not useful to tf log these
        values = to_tf_values(tmp, ["ray", "tune"])
        train_stats = tf.Summary(value=values)
        t = result.get(TIMESTEPS_TOTAL) or result[TRAINING_ITERATION]
        self._file_writer.add_summary(train_stats, t)
        iteration_value = to_tf_values({
            "training_iteration": result[TRAINING_ITERATION]
        }, ["ray", "tune"])
        iteration_stats = tf.Summary(value=iteration_value)
        self._file_writer.add_summary(iteration_stats, t)
        self._file_writer.flush()

    def flush(self):
        self._file_writer.flush()

    def close(self):
        self._file_writer.close()


class _VisKitLogger(Logger):
    def _init(self):
        """CSV outputted with Headers as first set of results."""
        # Note that we assume params.json was already created by JsonLogger
        self._file = open(os.path.join(self.logdir, "progress.csv"), "w")
        self._csv_out = None

    def on_result(self, result):
        if self._csv_out is None:
            self._csv_out = csv.DictWriter(self._file, result.keys())
            self._csv_out.writeheader()
        self._csv_out.writerow(result.copy())

    def close(self):
        self._file.close()


class _SafeFallbackEncoder(json.JSONEncoder):
    def __init__(self, nan_str="null", **kwargs):
        super(_SafeFallbackEncoder, self).__init__(**kwargs)
        self.nan_str = nan_str

    def default(self, value):
        try:
            if np.isnan(value):
                return None
            if np.issubdtype(value, float):
                return float(value)
            if np.issubdtype(value, int):
                return int(value)
        except Exception:
            return str(value)  # give up, just stringify it (ok for logs)


def pretty_print(result):
    result = result.copy()
    result.update(config=None)  # drop config from pretty print
    out = {}
    for k, v in result.items():
        if v is not None:
            out[k] = v

    cleaned = json.dumps(out, cls=_SafeFallbackEncoder)
    return yaml.safe_dump(json.loads(cleaned), default_flow_style=False)
