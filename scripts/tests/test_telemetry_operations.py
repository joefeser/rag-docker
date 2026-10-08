"""Operational #284 counts/correlation using real SDKs and synthetic producers."""
import asyncio
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2] / 'api')))
from services import telemetry as ot
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import View, DropAggregation, ExplicitBucketHistogramAggregation
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from test_telemetry import receiver, SECRET
import httpx
from fastapi import FastAPI


def dimensions(point):
    return {a.key: a.value.string_value for a in point.attributes}


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reader = InMemoryMetricReader()
        self.provider = MeterProvider(shutdown_on_exit=False, metric_readers=[self.reader], views=[
            View(instrument_name='*', aggregation=DropAggregation()),
            *[View(instrument_name=n, attribute_keys=set(v[2]), aggregation=ExplicitBucketHistogramAggregation(ot.BUCKETS) if v[0]=='create_histogram' else None) for n,v in ot.METRICS.items()]])
        self.runtime = ot.Runtime(ot.Config())
        self.runtime.meter = ot.SafeMeter(self.provider.get_meter('test'))
        self.logs = []
        self.runtime.logger = NS(emit=lambda **kw: self.logs.append(kw))

    def tearDown(self):
        self.provider.shutdown()

    def metrics(self):
        data = self.reader.get_metrics_data()
        return {} if data is None else {m.name: m for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics}

    def total(self, name, key='value'):
        return sum(getattr(p,key) for p in self.metrics()[name].data.data_points)

    async def test_exact_job_dependency_deltas_and_nested_outcomes(self):
        # Unknown token usage is absent. Stage spans do not add job counts.
        with ot.bind((self.runtime,None)), ot.span('rag.export'):
            with ot.span('rag.package'):
                ot.call('weaviate.query', lambda: None)
        with ot.bind((self.runtime,None)), ot.span('rag.import'):
            try:
                ot.call('ollama.embed', lambda: (_ for _ in ()).throw(TimeoutError(SECRET)))
            except TimeoutError:
                ot.outcome('error', TimeoutError(SECRET))
        with ot.bind((self.runtime,None)), ot.span('rag.ingest'):
            ot.outcome('partial')
        with self.assertRaises(asyncio.CancelledError), ot.bind((self.runtime,None)), ot.span('rag.evaluation'):
            raise asyncio.CancelledError()
        self.assertEqual(self.total('rag.job.completed'),4)
        self.assertEqual(self.total('rag.job.duration','count'),4)
        self.assertEqual(self.total('rag.job.active'),0)
        self.assertEqual(self.total('rag.dependency.calls'),2)
        self.assertEqual(self.total('rag.dependency.errors'),1)
        outcomes={p.attributes['rag.operation']:p.attributes['rag.outcome'] for p in self.metrics()['rag.job.completed'].data.data_points}
        self.assertEqual(outcomes,{'export':'ok','import':'error','ingest':'error','evaluation':'cancelled'})
        self.assertEqual(len(self.logs),6)
        self.assertTrue(all('rag.job_token' in log['attributes'] for log in self.logs))
        self.assertEqual(self.logs[0]['attributes']['rag.job_token'], self.logs[1]['attributes']['rag.job_token'])
        self.assertNotIn('token.usage',self.metrics())

    async def test_deterministic_monotonic_duration(self):
        with patch.object(ot.time,'monotonic',side_effect=[10,12.5]), ot.bind((self.runtime,None)), ot.span('rag.export'):
            pass
        self.assertEqual(self.total('rag.job.duration','sum'),2.5)
        self.assertEqual(self.total('rag.job.active'),0)

    async def test_real_asgi_routes_failures_stream_cancel_and_unknown_methods(self):
        app=FastAPI()
        app.state.telemetry=self.runtime
        app.add_middleware(ot.RequestTracing)
        @app.get('/health')
        async def health(): return {'ok': True}
        @app.post('/query')
        async def query(): raise ValueError(SECRET)
        entered=asyncio.Event()
        from fastapi.responses import StreamingResponse
        @app.get('/stream')
        async def stream():
            async def chunks():
                yield b'a'; entered.set(); await asyncio.sleep(60)
            return StreamingResponse(chunks())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
            self.assertEqual((await client.get('/health')).status_code,200)
            self.assertEqual((await client.post('/query')).status_code,500)
            self.assertEqual((await client.request('PRIVATE', '/'+SECRET)).status_code,404)
            task=asyncio.create_task(client.get('/stream'))
            await entered.wait()
            self.assertEqual(self.total('rag.api.active'),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.total('rag.api.requests'),4)
        self.assertEqual(self.total('rag.api.duration','count'),4)
        self.assertEqual(self.total('rag.api.active'),0)
        points=self.metrics()['rag.api.requests'].data.data_points
        labels=[dict(p.attributes) for p in points]
        self.assertIn({'http.route':'POST /query','http.method':'POST','http.status_class':'unknown','rag.outcome':'error'},labels)
        self.assertTrue(any(p['http.method']=='unknown' for p in labels))
        self.assertTrue(any(p['rag.outcome']=='cancelled' for p in labels))
        self.assertNotIn(SECRET,str(labels))

    async def test_cardinality_before_aggregation_and_concurrent_job_tokens(self):
        instrument=self.runtime.meter.create_counter('rag.api.requests')
        attrs={'http.route':'GET /health','http.method':'GET','http.status_class':'2xx','rag.outcome':'ok'}
        for n in range(1000):
            instrument.add(1,{**attrs,'job_id':str(n),'rag.job_token':f'{n:032x}','filename':SECRET})
            instrument.add(1,{**attrs,'http.route':SECRET+str(n)})
        self.assertEqual(len(self.metrics()['rag.api.requests'].data.data_points),1)
        self.assertEqual(self.total('rag.api.requests'),1000)
        async def job():
            with ot.span('rag.export'):
                await asyncio.sleep(0)
                ot.call('weaviate.query',lambda:None)
        with ot.bind((self.runtime,None)):
            await asyncio.gather(*(job() for _ in range(20)))
        logs=[r for r in self.logs if r['body']=='rag.job.completed']
        tokens={r['attributes']['rag.job_token'] for r in logs}
        self.assertEqual(len(tokens),20)
        self.assertEqual({r['attributes']['rag.job_token'] for r in self.logs if r['body']=='rag.dependency.completed'},tokens)
        self.assertEqual(len(self.metrics()['rag.job.completed'].data.data_points),1)
        self.assertIsNone(ot._job_token.get())
        self.assertIsNone(ot._operation.get())

    async def test_worker_waiter_cancellation_and_never_started_task(self):
        entered=threading.Event();release=threading.Event()
        @ot.traced('rag.export')
        def worker():
            entered.set();release.wait(3)
        with ot.bind((self.runtime,None)):
            captured=ot.admitted(worker)
        task=asyncio.create_task(asyncio.to_thread(captured))
        await asyncio.to_thread(entered.wait,2)
        self.assertEqual(self.total('rag.job.active'),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.total('rag.job.active'),1)
        self.assertNotIn('rag.job.completed',self.metrics())
        release.set()
        for _ in range(1000):
            if 'rag.job.completed' in self.metrics(): break
            await asyncio.sleep(.001)
        self.assertEqual(self.total('rag.job.completed'),1)
        self.assertEqual(self.total('rag.job.active'),0)
        @ot.traced('rag.evaluation')
        async def never(): raise AssertionError('must never execute')
        with ot.bind((self.runtime,None)):
            task=asyncio.create_task(ot.admitted(never)())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.total('rag.job.completed'),1)

    async def test_iterator_lifetime_no_context_leak_and_early_close(self):
        def rows():
            yield 1
            raise ConnectionError(SECRET)
        with ot.bind((self.runtime,None)),ot.span('rag.export'):
            iterator=ot.iterate(rows)
            self.assertEqual(next(iterator),1)
            self.assertEqual(ot._operation.get().category,'job')
            with self.assertRaises(ConnectionError):next(iterator)
            closed=ot.iterate(lambda:iter([1,2]))
            next(closed);closed.close()
            ot.outcome('partial')
        self.assertEqual(self.total('rag.dependency.calls'),2)
        self.assertEqual(self.total('rag.dependency.errors'),1)
        self.assertEqual(self.total('rag.job.completed'),1)
        self.assertEqual(self.logs[-1]['attributes']['rag.outcome'],'partial')
        self.assertEqual({r['attributes']['rag.outcome'] for r in self.logs[:-1]},{'error','cancelled'})

    async def test_sampling_zero_and_sdk_faults_preserve_operation(self):
        provider=TracerProvider(sampler=TraceIdRatioBased(0),shutdown_on_exit=False)
        self.runtime.tracer=provider.get_tracer('test')
        try:
            with ot.bind((self.runtime,None)),ot.span('rag.export'):
                pass
            self.assertEqual(self.total('rag.job.completed'),1)
            self.assertEqual(len(self.logs),1)
            def fail(*args,**kwargs):raise RuntimeError(SECRET)
            self.runtime.meter=NS(create_counter=fail,create_up_down_counter=fail,create_histogram=fail)
            self.runtime.logger=NS(emit=fail)
            self.runtime.tracer=NS(start_span=fail)
            calls=[]
            with ot.bind((self.runtime,None)):
                self.assertEqual(ot.call('weaviate.query',lambda:calls.append(1) or 42),42)
                self.assertEqual(list(ot.iterate(lambda:iter([1,2]))),[1,2])
            self.assertEqual(calls,[1])
        finally:provider.shutdown()

    async def test_real_wire_all_instruments_multiple_series_and_log_correlation(self):
        with receiver() as (env,records):
            # Tiny trace/log batches must not truncate the set of metrics.
            runtime=ot.bootstrap({**env,'RAG_OTEL_BATCH_SIZE':'1'})
            try:
                for operation in ('export','import'):
                    with ot.bind((runtime,None)),ot.span('rag.request',server=True,method='GET'):
                        ot.attribute('http.route','GET /health')
                        ot.attribute('http.status_class','2xx')
                        with ot.span('rag.'+operation):
                            try:
                                ot.call('weaviate.query',lambda:(_ for _ in ()).throw(TimeoutError(SECRET)))
                            except TimeoutError: pass
                runtime.force_flush()
            finally:runtime.shutdown()
        metrics=[];logs=[]
        for path,payload in records:
            self.assertNotIn(SECRET.encode(),payload)
            if path=='/v1/metrics':metrics=ExportMetricsServiceRequest.FromString(payload).resource_metrics[0].scope_metrics[0].metrics
            if path=='/v1/logs':logs.extend(ExportLogsServiceRequest.FromString(payload).resource_logs[0].scope_logs[0].log_records)
        by_name={m.name:m for m in metrics}
        self.assertEqual(set(by_name),set(ot.METRICS)-{'rag.telemetry.check'})
        self.assertEqual(len(by_name['rag.job.completed'].sum.data_points),2)
        self.assertEqual(by_name['rag.job.duration'].unit,'s')
        for m in metrics:
            for p in getattr(m,m.WhichOneof('data')).data_points:
                self.assertNotIn('rag.job_token',dimensions(p));self.assertFalse(p.exemplars)
        job_logs=[r for r in logs if r.body.string_value=='rag.job.completed']
        self.assertEqual(len(job_logs),2)
        for log in job_logs:
            attrs=dimensions(log)
            self.assertRegex(attrs['rag.job_token'],r'^[0-9a-f]{32}$')
            self.assertEqual(len(log.trace_id),16);self.assertEqual(len(log.span_id),8)
            children=[r for r in logs if dimensions(r).get('rag.job_token')==attrs['rag.job_token']]
            self.assertEqual(len(children),2)
            self.assertEqual({r.trace_id for r in children},{log.trace_id})

    async def test_wire_admission_rejects_bad_records_without_consuming_names(self):
        from opentelemetry.proto.metrics.v1.metrics_pb2 import Metric
        def numeric():
            metric=Metric(name='rag.job.completed')
            metric.sum.aggregation_temporality=2;metric.sum.is_monotonic=True
            for operation in ('export','import'):
                point=metric.sum.data_points.add(as_int=1)
                for k,v in {'rag.operation':operation,'rag.outcome':'ok'}.items():
                    point.attributes.add(key=k).value.string_value=v
            return metric
        def histogram(**changes):
            metric=Metric(name='rag.job.duration')
            metric.histogram.aggregation_temporality=2
            values=dict(count=1,sum=0.001,min=0.001,max=0.001,
                        bucket_counts=[1]+[0]*len(ot.BUCKETS),explicit_bounds=ot.BUCKETS)
            values.update(changes)
            for operation in ('export','import'):
                point=metric.histogram.data_points.add(**values)
                for k,v in {'rag.operation':operation,'rag.outcome':'ok'}.items():
                    point.attributes.add(key=k).value.string_value=v
            return metric
        invalid=[]
        duplicate=numeric();duplicate.sum.data_points[1].CopyFrom(duplicate.sum.data_points[0]);invalid.append(duplicate)
        missing=numeric();missing.sum.data_points[1].ClearField('as_int');invalid.append(missing)
        for value in (float('nan'),float('inf'),-1):
            bad=numeric();bad.sum.data_points[1].as_double=value;invalid.append(bad)
        bad=numeric();bad.sum.data_points[1].attributes[0].value.string_value=SECRET;invalid.append(bad)
        bad=numeric();bad.sum.data_points[1].attributes.add().CopyFrom(bad.sum.data_points[1].attributes[0]);invalid.append(bad)
        bad=numeric();bad.sum.aggregation_temporality=0;invalid.append(bad)
        bad=numeric();bad.sum.is_monotonic=False;invalid.append(bad)
        for changes in (dict(sum=float('nan')),dict(min=float('-inf')),dict(max=float('inf')),
                        dict(min=3,max=2),dict(count=2),dict(sum=-1),
                        dict(explicit_bounds=tuple(reversed(ot.BUCKETS))),
                        dict(explicit_bounds=[0.1]*len(ot.BUCKETS))):
            invalid.append(histogram(**changes))
        good_count,good_hist=numeric(),histogram()
        # All malformed points include an initially valid series: partial
        # admission must not reserve the name or discard later valid series.
        with receiver() as (env,records):
            config=ot.Config.from_env({**env,'RAG_OTEL_BATCH_SIZE':'1'})
            with ot._session('metrics',config) as session:
                for bad in invalid:
                    source=ExportMetricsServiceRequest()
                    metrics=source.resource_metrics.add().scope_metrics.add().metrics
                    for metric in (bad,good_count,good_hist):metrics.add().CopyFrom(metric)
                    self.assertEqual(session.post('',data=source.SerializeToString()).status_code,200)
                    output=ExportMetricsServiceRequest.FromString(records[-1][1]).resource_metrics[0].scope_metrics[0].metrics
                    self.assertEqual({m.name for m in output},{'rag.job.completed','rag.job.duration'})
                    by_name={m.name:m for m in output}
                    good_count.unit='{job}';good_hist.unit='s'
                    self.assertEqual(by_name['rag.job.completed'],good_count)
                    self.assertEqual(by_name['rag.job.duration'],good_hist)
                absent=histogram()
                for point in absent.histogram.data_points:
                    for field in ('sum','min','max'):point.ClearField(field)
                source=ExportMetricsServiceRequest()
                source.resource_metrics.add().scope_metrics.add().metrics.add().CopyFrom(absent)
                session.post('',data=source.SerializeToString())
                exported=ExportMetricsServiceRequest.FromString(records[-1][1]).resource_metrics[0].scope_metrics[0].metrics[0]
                self.assertEqual(len(exported.histogram.data_points),2)
                self.assertTrue(all(not p.HasField(f) for p in exported.histogram.data_points for f in ('sum','min','max')))

    async def test_disabled_and_export_failure_preserve_results(self):
        with patch.object(ot,'_session',side_effect=AssertionError('no exporter')):
            disabled=ot.bootstrap({})
            with ot.bind((disabled,None)):
                self.assertEqual(ot.call('weaviate.query',lambda:7),7)
        with receiver(500) as (env,records):
            runtime=ot.bootstrap({**env,'RAG_OTEL_TRACES':'false'})
            try:
                with ot.bind((runtime,None)),ot.span('rag.export'):
                    self.assertEqual(ot.call('weaviate.query',lambda:9),9)
                runtime.force_flush()
            finally:runtime.shutdown()
            self.assertTrue(any(path=='/v1/logs' for path,_ in records))
            self.assertTrue(any(path=='/v1/metrics' for path,_ in records))
            self.assertFalse(any(path=='/v1/traces' for path,_ in records))

# Reuse the exact real router/worker fixtures from #283, adding metric/log
# assertions at the producer boundaries rather than testing decorators alone.
import test_tracing as tracing_fixture


class ProducerOperationsTests(unittest.IsolatedAsyncioTestCase):
    spans = tracing_fixture.TracingTests.spans
    wire = tracing_fixture.TracingTests.wire
    until = tracing_fixture.TracingTests.until
    lineage = tracing_fixture.TracingTests.lineage

    async def asyncSetUp(self):
        await tracing_fixture.TracingTests.asyncSetUp(self)
        self.reader = InMemoryMetricReader()
        self.metric_provider = MeterProvider(shutdown_on_exit=False, metric_readers=[self.reader])
        self.runtime.meter = ot.SafeMeter(self.metric_provider.get_meter('producer-test'))
        self.logs = []
        self.runtime.logger = NS(emit=lambda **kw:self.logs.append(kw))

    async def asyncTearDown(self):
        self.metric_provider.shutdown()
        await tracing_fixture.TracingTests.asyncTearDown(self)

    def assert_jobs(self, expected):
        data=self.reader.get_metrics_data()
        metrics={m.name:m for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics}
        actual={(p.attributes['rag.operation'],p.attributes['rag.outcome']):p.value
                for p in metrics['rag.job.completed'].data.data_points}
        self.assertEqual(actual,expected)
        self.assertTrue(all(p.value==0 for p in metrics['rag.job.active'].data.data_points))
        logs=[r for r in self.logs if r['body']=='rag.job.completed']
        self.assertEqual(len(logs),sum(expected.values()))
        self.assertTrue(all('rag.job_token' in r['attributes'] for r in logs))

    async def test_real_ingest_partial_counts_error_without_rewriting_status(self):
        await tracing_fixture.TracingTests.test_ingest_real_executor_partial_failure(self)
        self.assert_jobs({('ingest','error'):1})
        self.assertEqual(next(r['attributes']['rag.outcome'] for r in self.logs if r['body']=='rag.job.completed'),'partial')

    async def test_real_import_and_tuning_caught_failure(self):
        await tracing_fixture.TracingTests.test_import_and_tuning_real_launchers_handled_failure(self)
        self.assert_jobs({('import','error'):1,('tuning','error'):1})

    async def test_real_export_success_and_isolated_tokens(self):
        await tracing_fixture.TracingTests.test_export_admission_concurrency_and_after_response(self)
        self.assert_jobs({('export','ok'):2})
        self.assertEqual(len({r['attributes']['rag.job_token'] for r in self.logs if r['body']=='rag.job.completed'}),2)

    async def test_real_evaluation_cancellation_and_failure(self):
        await tracing_fixture.TracingTests.test_generation_cancellation_and_failure_reporter(self)
        self.assert_jobs({('evaluation','cancelled'):1,('evaluation','error'):1})

if __name__=='__main__':unittest.main()
