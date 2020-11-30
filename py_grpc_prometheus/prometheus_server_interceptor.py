"""Interceptor a client call with prometheus"""

from timeit import default_timer

import grpc

import py_grpc_prometheus.grpc_utils as grpc_utils
from py_grpc_prometheus.server_metrics import GRPC_SERVER_HANDLED_COUNTER
from py_grpc_prometheus.server_metrics import GRPC_SERVER_HANDLED_HISTOGRAM
from py_grpc_prometheus.server_metrics import GRPC_SERVER_STARTED_COUNTER
from py_grpc_prometheus.server_metrics import GRPC_SERVER_STREAM_MSG_RECEIVED
from py_grpc_prometheus.server_metrics import GRPC_SERVER_STREAM_MSG_SENT
# Legacy metrics
from py_grpc_prometheus.server_metrics import LEGACY_GRPC_SERVER_HANDLED_LATENCY_SECONDS
from py_grpc_prometheus.server_metrics import LEGACY_GRPC_SERVER_HANDLED_TOTAL_COUNTER
from py_grpc_prometheus.server_metrics import LEGACY_GRPC_SERVER_MSG_RECEIVED_TOTAL_COUNTER
from py_grpc_prometheus.server_metrics import LEGACY_GRPC_SERVER_MSG_SENT_TOTAL_COUNTER
from py_grpc_prometheus.server_metrics import LEGACY_GRPC_SERVER_STARTED_TOTAL_COUNTER


class PromServerInterceptor(grpc.ServerInterceptor):

  def __init__(self, enable_handling_time_histogram=False, legacy=False):
    self._enable_handling_time_histogram = enable_handling_time_histogram
    self._legacy = legacy

  def intercept_service(self, continuation, handler_call_details):
    """
    Intercepts the server function calls.

    This implements referred to:
    https://github.com/census-instrumentation/opencensus-python/blob/master/opencensus/
    trace/ext/grpc/server_interceptor.py
    and
    https://grpc.io/grpc/python/grpc.html#service-side-interceptor
    """

    grpc_service_name, grpc_method_name, _ = grpc_utils.split_method_call(handler_call_details)

    def metrics_wrapper(behavior, request_streaming, response_streaming):
      def new_behavior(request_or_iterator, servicer_context):

        start = default_timer()
        grpc_type = grpc_utils.get_method_type(request_streaming, response_streaming)
        try:

          received_metric = GRPC_SERVER_STREAM_MSG_RECEIVED
          if self._legacy:
            received_metric = LEGACY_GRPC_SERVER_MSG_RECEIVED_TOTAL_COUNTER

          if request_streaming:
            request_or_iterator = grpc_utils.wrap_iterator_inc_counter(
              request_or_iterator,
              GRPC_SERVER_STREAM_MSG_RECEIVED,
              grpc_type,
              grpc_service_name,
              grpc_method_name)
            if not self._legacy:
              request_or_iterator = grpc_utils.wrap_iterator_inc_counter(
                request_or_iterator,
                GRPC_SERVER_STREAM_MSG_SENT,
                grpc_type,
                grpc_service_name,
                grpc_method_name)
          else:
            if self._legacy:
              LEGACY_GRPC_SERVER_STARTED_TOTAL_COUNTER.labels(
                grpc_type=grpc_type,
                grpc_service=grpc_service_name,
                grpc_method=grpc_method_name) \
                .inc()
            else:
              GRPC_SERVER_STARTED_COUNTER.labels(
                grpc_type=grpc_type,
                grpc_service=grpc_service_name,
                grpc_method=grpc_method_name) \
                .inc()

          # Invoke the original rpc behavior.
          response_or_iterator = behavior(request_or_iterator, servicer_context)

          if response_streaming:

            sent_metric = GRPC_SERVER_STREAM_MSG_SENT
            if self._legacy:
              sent_metric = LEGACY_GRPC_SERVER_MSG_SENT_TOTAL_COUNTER

            if not self._legacy:
              response_or_iterator = grpc_utils.wrap_iterator_inc_counter(
                response_or_iterator,
                GRPC_SERVER_STREAM_MSG_RECEIVED,
                grpc_type,
                grpc_service_name,
                grpc_method_name)

            response_or_iterator = grpc_utils.wrap_iterator_inc_counter(
              response_or_iterator,
              sent_metric,
              grpc_type,
              grpc_service_name,
              grpc_method_name)

          else:

            if self._legacy:
              LEGACY_GRPC_SERVER_HANDLED_TOTAL_COUNTER.labels(
                grpc_type=grpc_type,
                grpc_service=grpc_service_name,
                grpc_method=grpc_method_name,
                code=self._compute_status_code(servicer_context).name).inc()
            else:
              GRPC_SERVER_HANDLED_COUNTER.labels(
                grpc_type=grpc_type,
                grpc_service=grpc_service_name,
                grpc_method=grpc_method_name,
                code=self._compute_status_code(servicer_context).name).inc()

          return response_or_iterator
        except grpc.RpcError as e:

          if self._legacy:
            LEGACY_GRPC_SERVER_HANDLED_TOTAL_COUNTER.labels(
              grpc_type=grpc_type,
              grpc_service=grpc_service_name,
              grpc_method=grpc_method_name,
              code=self._compute_error_code(e)).inc()
          else:
            GRPC_SERVER_HANDLED_COUNTER.labels(
              grpc_type=grpc_type,
              grpc_service=grpc_service_name,
              grpc_method=grpc_method_name,
              code=self._compute_error_code(e)).inc()

          raise e

        finally:

          if not response_streaming:
            if self._legacy:
              LEGACY_GRPC_SERVER_HANDLED_LATENCY_SECONDS.labels(
                grpc_type=grpc_type,
                grpc_service=grpc_service_name,
                grpc_method=grpc_method_name) \
                .observe(max(default_timer() - start, 0))
            elif self._enable_handling_time_histogram:
              GRPC_SERVER_HANDLED_HISTOGRAM.labels(
                grpc_type=grpc_type,
                grpc_service=grpc_service_name,
                grpc_method=grpc_method_name) \
                .observe(max(default_timer() - start, 0))

      return new_behavior

    optional_any = self._wrap_rpc_behavior(continuation(handler_call_details), metrics_wrapper)

    return optional_any

  # pylint: disable=protected-access
  def _compute_status_code(self, servicer_context):
    if servicer_context._state.client == "cancelled":
      return grpc.StatusCode.CANCELLED

    if servicer_context._state.code is None:
      return grpc.StatusCode.OK

    return servicer_context._state.code

  def _compute_error_code(self, grpc_exception):
    if isinstance(grpc_exception, grpc.Call):
      return grpc_exception.code().name

    return grpc.StatusCode.UNKNOWN.name

  def _wrap_rpc_behavior(self, handler, fn):
    """Returns a new rpc handler that wraps the given function"""
    if handler is None:
      return None

    if handler.request_streaming and handler.response_streaming:
      behavior_fn = handler.stream_stream
      handler_factory = grpc.stream_stream_rpc_method_handler
    elif handler.request_streaming and not handler.response_streaming:
      behavior_fn = handler.stream_unary
      handler_factory = grpc.stream_unary_rpc_method_handler
    elif not handler.request_streaming and handler.response_streaming:
      behavior_fn = handler.unary_stream
      handler_factory = grpc.unary_stream_rpc_method_handler
    else:
      behavior_fn = handler.unary_unary
      handler_factory = grpc.unary_unary_rpc_method_handler

    return handler_factory(
      fn(behavior_fn, handler.request_streaming, handler.response_streaming),
      request_deserializer=handler.request_deserializer,
      response_serializer=handler.response_serializer)
