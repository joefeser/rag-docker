"""Real SDK/loopback OTLP acceptance for #282; no external service needed."""
import contextlib
import http.server
import io
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2] / 'api')))
from services.telemetry import Config, Runtime, TelemetryConfigError, bootstrap, sanitize_wire
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.trace import Status, StatusCode

SECRET = 'SENTINEL-secret-prompt-document-/private/file.txt'

@contextlib.contextmanager
def receiver(status=200):
    records = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            records.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(status)
            self.send_header('Location', 'http://127.0.0.1:1/' + SECRET)
            self.end_headers()
            self.wfile.write(SECRET.encode())
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': f'http://127.0.0.1:{server.server_port}',
               'RAG_OTEL_TIMEOUT_MS': '100', 'RAG_OTEL_SHUTDOWN_MS': '500'}, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

class TelemetryTests(unittest.TestCase):
    def test_disabled_never_constructs_transport_or_reads_secrets(self):
        with patch('services.telemetry._session', side_effect=AssertionError), patch('pathlib.Path.open', side_effect=AssertionError):
            runtime = bootstrap({'RAG_OTEL_HEADERS_FILE': SECRET, 'RAG_OTEL_ENDPOINT': SECRET})
            self.assertFalse(runtime.config.enabled)
            self.assertEqual(runtime.providers, [])
            self.assertTrue(runtime.force_flush())
            self.assertTrue(runtime.shutdown())

    def test_invalid_configuration_is_value_free(self):
        fields = {'ENABLED': '1', 'PROTOCOL': 'grpc', 'ENDPOINT': 'http://user:secret@example.com',
                  'SERVICE_NAME': SECRET, 'SAMPLE_RATIO': 'nan', 'QUEUE_SIZE': '0', 'BATCH_SIZE': '5000',
                  'TIMEOUT_MS': '0', 'INTERVAL_MS': '0', 'SHUTDOWN_MS': '999999', 'LOGS': 'yes'}
        for key, value in fields.items():
            env = {'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': 'http://localhost:4318', 'RAG_OTEL_'+key: value}
            with self.subTest(key=key), self.assertRaises(TelemetryConfigError) as raised:
                Config.from_env(env)
            self.assertNotIn(SECRET, str(raised.exception))
        for endpoint in ('http://localhost/?secret=yes', 'http://localhost/path', 'file:///tmp/a', 'http://localhost:99999'):
            with self.assertRaises(TelemetryConfigError):
                Config.from_env({'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': endpoint})

    def test_header_file_and_repr(self):
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('{"Authorization":"'+SECRET+'"}');f.flush()
            cfg = Config.from_env({'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': 'https://localhost:4318', 'RAG_OTEL_HEADERS_FILE': f.name})
            self.assertEqual(cfg.headers['Authorization'], SECRET)
            self.assertNotIn(SECRET, repr(cfg))
        with self.assertRaises(TelemetryConfigError) as err:
            Config.from_env({'RAG_OTEL_ENABLED': 'true', 'RAG_OTEL_ENDPOINT': 'https://localhost', 'RAG_OTEL_HEADERS_FILE': SECRET})
        self.assertNotIn(SECRET, str(err.exception))

    def test_real_sdk_all_signals_safe_on_receiver(self):
        with receiver() as (env, records), patch.dict(os.environ, {'OTEL_RESOURCE_ATTRIBUTES': 'secret='+SECRET,
                 'OTEL_SERVICE_NAME': SECRET, 'OTEL_EXPORTER_OTLP_HEADERS': 'authorization='+SECRET}):
            runtime = bootstrap(env)
            try:
                with runtime.tracer.start_as_current_span(SECRET, attributes={'rag.operation': 'query', 'prompt': SECRET, 'rag.outcome': SECRET}) as span:
                    span.add_event(SECRET, {'secret': SECRET})
                    span.set_status(Status(StatusCode.ERROR, SECRET))
                    runtime.logger.emit(body=SECRET, severity_text=SECRET, attributes={'rag.outcome': 'error', 'secret': SECRET})
                counter = runtime.meter.create_counter('rag.telemetry.check', description=SECRET, unit=SECRET)
                for n in range(200):
                    counter.add(1, {'secret': SECRET+str(n), 'trace_id': str(n)})
                runtime.meter.create_counter('secret.metric').add(1, {'secret': SECRET})
                self.assertTrue(runtime.force_flush())
            finally:
                self.assertTrue(runtime.shutdown())
            by_path = dict(records)
            self.assertEqual(set(by_path), {'/v1/traces', '/v1/logs', '/v1/metrics'})
            for body in by_path.values():
                self.assertNotIn(SECRET.encode(), body)
            traces = ExportTraceServiceRequest.FromString(by_path['/v1/traces'])
            resource = {a.key: a.value.string_value for a in traces.resource_spans[0].resource.attributes}
            self.assertEqual(resource, {'service.name':'rag-api', 'service.version':'1.1.0', 'deployment.environment.name':'development'})
            span = traces.resource_spans[0].scope_spans[0].spans[0]
            self.assertEqual(span.name, 'rag.operation')
            self.assertFalse(span.events);self.assertFalse(span.links);self.assertFalse(span.status.message)
            logs = ExportLogsServiceRequest.FromString(by_path['/v1/logs'])
            log = logs.resource_logs[0].scope_logs[0].log_records[0]
            self.assertEqual(log.trace_id, span.trace_id)
            self.assertEqual(log.body.string_value, 'rag.operation')
            metrics = ExportMetricsServiceRequest.FromString(by_path['/v1/metrics'])
            metric = metrics.resource_metrics[0].scope_metrics[0].metrics[0]
            self.assertEqual(metric.name, 'rag.telemetry.check')
            self.assertEqual(len(metric.sum.data_points), 1)
            self.assertFalse(metric.sum.data_points[0].attributes)
            self.assertEqual(metric.sum.data_points[0].as_int, 200)

    def test_wire_rebuilds_scope_resource_and_free_text(self):
        source = ExportTraceServiceRequest()
        rs = source.resource_spans.add(schema_url=SECRET)
        rs.resource.attributes.add(key='secret').value.string_value = SECRET
        ss = rs.scope_spans.add(schema_url=SECRET);ss.scope.name=SECRET;ss.scope.version=SECRET
        ss.scope.attributes.add(key='secret').value.string_value=SECRET
        span=ss.spans.add(name=SECRET, trace_state=SECRET)
        span.status.message=SECRET
        span.events.add(name=SECRET)
        span.links.add(trace_state=SECRET)
        for key in ('rag.operation','authorization','filename','prompt','exception.stacktrace'):
            span.attributes.add(key=key).value.string_value=SECRET
        body=sanitize_wire(source.SerializeToString(), 'traces', Config())
        self.assertNotIn(SECRET.encode(),body)

    def test_histogram_large_shape_is_dropped_not_truncated(self):
        source=ExportMetricsServiceRequest()
        metric=source.resource_metrics.add().scope_metrics.add().metrics.add(name='rag.telemetry.check')
        point=metric.histogram.data_points.add(count=40, sum=40)
        point.bucket_counts.extend([1]*40);point.explicit_bounds.extend(range(39))
        result=ExportMetricsServiceRequest.FromString(sanitize_wire(source.SerializeToString(),'metrics',Config()))
        self.assertFalse(result.resource_metrics[0].scope_metrics[0].metrics[0].histogram.data_points)

    def test_bounded_queue_under_blocked_export(self):
        from opentelemetry.sdk.trace.export import SpanExportResult
        entered=threading.Event();release=threading.Event()
        def blocked(*args):
            entered.set();release.wait(2);return SpanExportResult.FAILURE
        with patch('opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter.export',side_effect=blocked):
            runtime=bootstrap({'RAG_OTEL_ENABLED':'true','RAG_OTEL_ENDPOINT':'http://localhost:1',
                               'RAG_OTEL_LOGS':'false','RAG_OTEL_METRICS':'false','RAG_OTEL_BATCH_SIZE':'1','RAG_OTEL_QUEUE_SIZE':'4'})
            try:
                with runtime.tracer.start_as_current_span('rag.query'):pass
                self.assertTrue(entered.wait(1))
                for _ in range(20):
                    with runtime.tracer.start_as_current_span(SECRET):pass
                processor=runtime.providers[0]._active_span_processor._span_processors[0]._batch_processor
                self.assertEqual(len(processor._queue),4)
                self.assertTrue(all(s.name=='rag.operation' for s in processor._queue))
            finally:release.set();runtime.shutdown()

    def test_outage_no_retry_or_sensitive_diagnostics(self):
        for status in (500, 429, 302):
            with receiver(status) as (env, records):
                stream=io.StringIO();handler=logging.StreamHandler(stream)
                logger=logging.getLogger('opentelemetry');logger.addHandler(handler)
                try:
                    runtime=bootstrap({**env, 'RAG_OTEL_LOGS':'false','RAG_OTEL_METRICS':'false'})
                    started=time.monotonic()
                    with runtime.tracer.start_as_current_span('rag.query'):pass
                    runtime.force_flush();runtime.shutdown()
                    self.assertLess(time.monotonic()-started,1.5)
                    self.assertEqual(len(records),1)
                    self.assertNotIn(SECRET,stream.getvalue())
                    self.assertNotIn(env['RAG_OTEL_ENDPOINT'],stream.getvalue())
                finally:logger.removeHandler(handler)

    def test_sampling_zero_and_signal_toggles(self):
        with receiver() as (env, records):
            runtime=bootstrap({**env,'RAG_OTEL_SAMPLE_RATIO':'0','RAG_OTEL_LOGS':'false','RAG_OTEL_METRICS':'false'})
            with runtime.tracer.start_as_current_span('rag.query'):pass
            self.assertIsNone(runtime.logger);self.assertIsNone(runtime.meter)
            runtime.force_flush();runtime.shutdown();runtime.shutdown()
            self.assertFalse(records)

    def test_api_lifespan_cleans_up_on_failed_startup_and_normal_exit(self):
        import ast
        import asyncio
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        api=Path(os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2]/'api')))
        tree=ast.parse((api/'main.py').read_text())
        function=next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name=='lifespan')
        namespace={'asynccontextmanager':asynccontextmanager,'FastAPI':object}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'main.py','exec'),namespace)
        for fail in (False, True):
            order=[]
            runtime=SimpleNamespace(shutdown=lambda:order.append('shutdown'))
            gold=SimpleNamespace(load_sessions_from_disk=Mock(side_effect=RuntimeError('startup') if fail else lambda:order.append('load')),
                                 reconcile_interrupted_generations=Mock())
            wc=SimpleNamespace(sweep_staging=AsyncMock(return_value=[]),close_client=lambda:order.append('close'))
            importer=SimpleNamespace(sweep_interrupted_imports=lambda:[],sweep_stale_workdirs=lambda:[])
            service=SimpleNamespace(goldstandard=gold,metrics=SimpleNamespace(load_from_disk=Mock()),weaviate_client=wc,importer=importer)
            modules={'services':service,'services.telemetry':SimpleNamespace(bootstrap=lambda:(order.append('bootstrap') or runtime))}
            async def exercise():
                async with namespace['lifespan'](SimpleNamespace(state=SimpleNamespace())):
                    order.append('yield')
            with patch.dict(sys.modules,modules):
                if fail:
                    with self.assertRaises(RuntimeError):asyncio.run(exercise())
                else:asyncio.run(exercise())
            self.assertEqual(order[0],'bootstrap')
            self.assertEqual(order[-2:],['close','shutdown'])
            self.assertEqual('yield' in order,not fail)

    def test_lifecycle_deadline_single_worker_and_eventual_shutdown(self):
        release=threading.Event()
        class Stalled:
            closed=0
            def force_flush(self, **kwargs):release.wait();return True
            def shutdown(self):self.closed+=1
        runtime=Runtime(Config(enabled=True,shutdown_ms=100));provider=Stalled();runtime.providers=[provider]
        try:
            started=time.monotonic()
            self.assertFalse(runtime.force_flush())
            worker=runtime._worker
            self.assertFalse(runtime.shutdown())
            self.assertIs(worker,runtime._worker)
            self.assertLess(time.monotonic()-started,.5)
        finally:release.set();worker.join(1)
        self.assertEqual(provider.closed,1)
        self.assertTrue(runtime.shutdown())
        self.assertEqual(provider.closed,1)

if __name__ == '__main__':unittest.main()
