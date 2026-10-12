#!/usr/bin/env python3
"""TCP transport benchmark; defaults to isolated loopback services only.

Run in a disposable environment with grpcio, grpcio-tools, aiohttp, requests,
httpx, protobuf and PyYAML. Results are synthetic, not robot acceptance evidence.
The server and delay forwarders bind only to loopback and run in a child process.
Explicit --probe-configured performs health GETs on configured services instead.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = '''syntax = "proto3";
package bench;
message Packet {
  string command_id = 1; string task_id = 2; string state = 3;
  repeated double goal_xyt = 4; uint64 completed_ns = 5;
  bool success = 6; bool reached = 7; bool navigation_stopped = 8;
  string diagnostic = 9; int32 delay_ms = 10;
}
message JsonPacket { bytes data = 1; }
service Transport {
  rpc Echo(Packet) returns (Packet);
  rpc EchoJson(JsonPacket) returns (JsonPacket);
  rpc Submit(Packet) returns (Packet);
  rpc Get(Packet) returns (Packet);
  rpc Watch(Packet) returns (stream Packet);
}
'''


def modules(directory):
    sys.path.insert(0, str(directory))
    return importlib.import_module('bench_pb2'), importlib.import_module('bench_pb2_grpc')


def packet_dict(packet):
    return {name: (list(getattr(packet, name)) if name == 'goal_xyt' else getattr(packet, name))
            for name in ('command_id', 'task_id', 'state', 'goal_xyt', 'completed_ns',
                         'success', 'reached', 'navigation_stopped', 'diagnostic', 'delay_ms')}


def sample(size=256):
    rng = random.Random(631 + size)
    return dict(command_id='nav-table2-benchmark', task_id='isolated-benchmark',
                state='succeeded', goal_xyt=[0.9903405869861586, 1.3761315438191244, -.39236607751253016],
                completed_ns=0, success=True, reached=True, navigation_stopped=True,
                diagnostic=''.join(rng.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=size)), delay_ms=0)


def summary(values):
    ordered = sorted(values)
    def percentile(p):
        return ordered[min(len(ordered)-1, max(0, int((len(ordered)-1)*p + .5)))]
    return dict(n=len(values), mean_ms=statistics.mean(values), p50_ms=percentile(.5),
                p95_ms=percentile(.95), p99_ms=percentile(.99), max_ms=max(values))


def probe_configured(output, rounds, count):
    """Explicit opt-in: health GETs only, no deployment or command operations."""
    import requests
    import yaml
    sys.path.insert(0, str(ROOT / 'src'))
    from brain.adapters.http_client import HttpClient
    network = yaml.safe_load((ROOT/'config/networks.yaml').read_text())
    active = network['wifis'][network['active']]
    configured = {name: f"http://{active[role]}:{network['roles'][role]['http_port']}"
                  for name, role in [('nav_real', 'nav'), ('vla_real', 'control')]}
    profile = json.loads((ROOT/'data/system/execution.json').read_text())
    applied = profile['routes']['reception'].get('endpoints', {})
    configured.update({'applied_'+name: url for name, url in applied.items()})
    probes, reachable = [], {}
    for name, base in configured.items():
        session = requests.Session()
        session.trust_env = False
        start = time.perf_counter()
        try:
            response = session.get(base+'/health', timeout=(2, 3))
            probes.append(dict(label=name, status=response.status_code,
                               ms=(time.perf_counter()-start)*1000, bytes=len(response.content),
                               connection=response.headers.get('Connection'),
                               server=response.headers.get('Server')))
            if response.status_code == 200:
                reachable[name] = base
        except requests.RequestException as exc:
            probes.append(dict(label=name, error=type(exc).__name__,
                               ms=(time.perf_counter()-start)*1000))
        finally:
            session.close()
    (output/'live_probe.json').write_text(json.dumps(dict(time=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                                        tests=probes), indent=2))
    rows = []
    for role, base in reachable.items():
        for round_index in range(rounds):
            modes = ['current_urllib', 'requests_pool']
            random.Random(round_index).shuffle(modes)
            for mode in modes:
                session = requests.Session()
                session.trust_env = False
                client = HttpClient(base, timeout_sec=3)
                samples, errors, versions, closes = [], [], [], []
                for index in range(count+2):
                    start = time.perf_counter()
                    try:
                        if mode == 'current_urllib':
                            client.request_json('GET', '/health')
                        else:
                            with session.get(base+'/health', timeout=(2, 3), stream=True) as response:
                                response.raise_for_status()
                                versions.append(response.raw.version)
                                closes.append(response.raw._original_response.will_close)
                                response.json()
                        if index >= 2:
                            samples.append((time.perf_counter()-start)*1000)
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                    time.sleep(.1)
                session.close()
                row = dict(role=role, round=round_index, mode=mode, n=len(samples),
                           samples_ms=samples, errors=errors, http_versions=sorted(set(versions)),
                           response_will_close=sorted(set(closes)))
                rows.append(row)
                print(json.dumps({k:v for k,v in row.items() if k != 'samples_ms'}), flush=True)
    (output/'live_http.json').write_text(json.dumps(dict(time=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        scope='configured health GET only; sequential; 100 ms pause; 2 warmups per group', rows=rows), indent=2))


async def server(args):
    import grpc
    from aiohttp import web
    pb, rpc = modules(args.generated)
    commands, finished, tasks = {}, {}, set()
    connections = {}

    def connection_id(request):
        peer = request.transport.get_extra_info('peername')
        if peer not in connections:
            connections[peer] = len(connections) + 1
        return str(connections[peer])

    async def complete(command_id, delay_ms):
        await asyncio.sleep(delay_ms / 1000)
        commands[command_id].update(state='succeeded', success=True, reached=True,
                                    navigation_stopped=True, completed_ns=time.perf_counter_ns())
        finished[command_id].set()

    def submit(data):
        command_id = data['command_id']
        if command_id not in commands:
            commands[command_id] = dict(data, state='accepted', completed_ns=0,
                                         success=False, reached=False, navigation_stopped=False)
            finished[command_id] = asyncio.Event()
            task = asyncio.create_task(complete(command_id, data['delay_ms']))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return dict(commands[command_id])

    class Service(rpc.TransportServicer):
        async def Echo(self, request, context):
            return pb.Packet(**packet_dict(request))

        async def EchoJson(self, request, context):
            return pb.JsonPacket(data=json.dumps(json.loads(request.data)).encode())

        async def Submit(self, request, context):
            return pb.Packet(**submit(packet_dict(request)))

        async def Get(self, request, context):
            if request.command_id not in commands:
                await context.abort(grpc.StatusCode.NOT_FOUND, 'unknown command')
            return pb.Packet(**commands[request.command_id])

        async def Watch(self, request, context):
            if request.command_id not in commands:
                await context.abort(grpc.StatusCode.NOT_FOUND, 'unknown command')
            await finished[request.command_id].wait()
            yield pb.Packet(**commands[request.command_id])

    async def echo(request):
        data = await request.json()
        return web.json_response(data, headers={'X-Bench-Connection': connection_id(request)})

    async def post(request):
        return web.json_response(submit(await request.json()), status=202)

    async def get(request):
        return web.json_response(commands[request.match_info['command_id']])

    async def events(request):
        command_id = request.match_info['command_id']
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        await finished[command_id].wait()
        await response.write(('id: 1\ndata: ' + json.dumps(commands[command_id]) + '\n\n').encode())
        await response.write_eof()
        return response

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            command_id = json.loads(msg.data)['command_id']
            await finished[command_id].wait()
            await ws.send_json(commands[command_id])
        return ws

    app = web.Application()
    app.add_routes([web.post('/echo', echo), web.post('/commands', post),
                    web.get('/commands/{command_id}', get),
                    web.get('/events/{command_id}', events), web.get('/ws', websocket)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    http_port = site._server.sockets[0].getsockname()[1]
    grpc_server = grpc.aio.server()
    rpc.add_TransportServicer_to_server(Service(), grpc_server)
    grpc_port = grpc_server.add_insecure_port('127.0.0.1:0')
    await grpc_server.start()
    proxies = []

    async def forwarding(reader, writer, target, delay):
        upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', target)
        async def direction(source, dest):
            # Timestamp each read, then delay forwarding without rate-limiting reads.
            queue = asyncio.Queue()
            async def receive():
                while True:
                    chunk = await source.read(65536)
                    await queue.put((asyncio.get_running_loop().time() + delay, chunk))
                    if not chunk:
                        break
            receiver = asyncio.create_task(receive())
            try:
                while True:
                    when, chunk = await queue.get()
                    if not chunk:
                        break
                    await asyncio.sleep(max(0, when - asyncio.get_running_loop().time()))
                    dest.write(chunk)
                    await dest.drain()
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
        pumps = [asyncio.create_task(direction(reader, upstream_writer)),
                 asyncio.create_task(direction(upstream_reader, writer))]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            upstream_writer.close()
            writer.close()

    endpoints = {'0': {'http': http_port, 'grpc': grpc_port}}
    for rtt_ms in (10, 50):
        endpoints[str(rtt_ms)] = {}
        for protocol, port in [('http', http_port), ('grpc', grpc_port)]:
            async def accept(reader, writer, target=port, delay=rtt_ms/2000):
                await forwarding(reader, writer, target, delay)
            proxy = await asyncio.start_server(accept, '127.0.0.1', 0)
            proxies.append(proxy)
            endpoints[str(rtt_ms)][protocol] = proxy.sockets[0].getsockname()[1]
    Path(args.ready).write_text(json.dumps(endpoints))
    try:
        await asyncio.Event().wait()
    finally:
        await grpc_server.stop(0)
        await runner.cleanup()
        for proxy in proxies:
            proxy.close()


def unary(endpoints, generated, rounds):
    import grpc
    import requests
    import httpx
    sys.path.insert(0, str(ROOT / 'src'))
    from brain.adapters.http_client import HttpClient
    pb, rpc = modules(generated)
    output = []
    modes = ['current_urllib', 'requests_pool', 'httpx_pool', 'grpc_json', 'grpc_protobuf']
    for rtt, sizes, count in [(0, (256, 4096, 65536), 160), (10, (256,), 60), (50, (256,), 24)]:
        ports = endpoints[str(rtt)]
        url = f"http://127.0.0.1:{ports['http']}"
        for size in sizes:
            data = sample(size)
            for round_index in range(rounds):
                order = list(modes)
                random.Random(42 + round_index).shuffle(order)
                for mode in order:
                    connections, samples = set(), []
                    session = None
                    channel = None
                    if mode == 'current_urllib':
                        client = HttpClient(url)
                        def call():
                            body, _, headers = client.request_json('POST', '/echo', data)
                            connections.add(headers['X-Bench-Connection'])
                            assert body == data
                    elif mode in ('requests_pool', 'httpx_pool'):
                        session = requests.Session() if mode == 'requests_pool' else httpx.Client(trust_env=False)
                        if mode == 'requests_pool':
                            session.trust_env = False
                        def call():
                            response = session.post(url + '/echo', json=data, timeout=5)
                            response.raise_for_status()
                            connections.add(response.headers['X-Bench-Connection'])
                            assert response.json() == data
                    else:
                        channel = grpc.insecure_channel(f"127.0.0.1:{ports['grpc']}")
                        grpc.channel_ready_future(channel).result(timeout=5)
                        stub = rpc.TransportStub(channel)
                        def call():
                            if mode == 'grpc_protobuf':
                                result = packet_dict(stub.Echo(pb.Packet(**data), timeout=5))
                            else:
                                result = json.loads(stub.EchoJson(pb.JsonPacket(data=json.dumps(data).encode()), timeout=5).data)
                            assert result == data
                    for _ in range(5):
                        call()
                    connections.clear()
                    cpu_before = time.process_time()
                    for _ in range(count):
                        start = time.perf_counter_ns()
                        call()
                        samples.append((time.perf_counter_ns() - start) / 1e6)
                    cpu_ms = (time.process_time() - cpu_before) * 1000
                    if session:
                        session.close()
                    if channel:
                        channel.close()
                    row = dict(kind='unary', mode=mode, added_rtt_ms=rtt, round=round_index,
                               diagnostic_bytes=size, json_bytes=len(json.dumps(data).encode()),
                               protobuf_bytes=len(pb.Packet(**data).SerializeToString()),
                               distinct_http_connections=len(connections) if connections else None,
                               client_cpu_ms=cpu_ms, samples_ms=samples, **summary(samples))
                    output.append(row)
                    print(json.dumps({k:v for k,v in row.items() if k != 'samples_ms'}), flush=True)
    return output


async def notifications(endpoints, generated, count, max_delay_ms, concurrency):
    import grpc
    import aiohttp
    pb, rpc = modules(generated)
    output = []
    modes = ['http_poll_500ms', 'http_poll_1000ms', 'http_poll_100ms',
             'http_sse', 'http_websocket_cold', 'http_websocket_preconnected',
             'grpc_stream', 'http_submit_grpc_stream']
    for rtt in (0, 10, 50):
        ports = endpoints[str(rtt)]
        url = f"http://127.0.0.1:{ports['http']}"
        for mode in modes:
            samples, query_counts, durations = [], [], []
            semaphore = asyncio.Semaphore(concurrency)
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with grpc.aio.insecure_channel(f"127.0.0.1:{ports['grpc']}") as channel:
                    stub = rpc.TransportStub(channel)
                    await channel.channel_ready()
                    async def one(index):
                        async with semaphore:
                            data = sample()
                            data.update(command_id=f'{rtt}-{mode}-{index}',
                                        delay_ms=random.Random(809 + index).randint(50, max_delay_ms))
                            ws = None
                            if mode == 'http_websocket_preconnected':
                                ws = await session.ws_connect(url + '/ws', compress=0)
                            start = time.perf_counter_ns()
                            if mode == 'grpc_stream':
                                await stub.Submit(pb.Packet(**data), timeout=5)
                            else:
                                async with session.post(url + '/commands', json=data) as response:
                                    assert response.status == 202
                                    await response.read()
                            queries = 0
                            if mode.startswith('http_poll_'):
                                interval = int(mode.removeprefix('http_poll_').removesuffix('ms')) / 1000
                                while True:
                                    async with session.get(url + '/commands/' + data['command_id']) as response:
                                        result = await response.json()
                                    queries += 1
                                    if result['state'] == 'succeeded':
                                        break
                                    await asyncio.sleep(interval)
                            elif mode == 'http_sse':
                                async with session.get(url + '/events/' + data['command_id']) as response:
                                    async for line in response.content:
                                        if line.startswith(b'data: '):
                                            result = json.loads(line[6:])
                                            break
                            elif mode.startswith('http_websocket_'):
                                if ws is None:
                                    ws = await session.ws_connect(url + '/ws', compress=0)
                                await ws.send_json({'command_id': data['command_id']})
                                result = await ws.receive_json(timeout=5)
                            else:
                                async for event in stub.Watch(pb.Packet(command_id=data['command_id']), timeout=5):
                                    result = packet_dict(event)
                            received = time.perf_counter_ns()
                            assert result['command_id'] == data['command_id']
                            assert result['success'] and result['navigation_stopped']
                            assert received >= result['completed_ns'] > start
                            samples.append((received - result['completed_ns'])/1e6)
                            durations.append((received - start)/1e6)
                            query_counts.append(queries)
                            if ws is not None:
                                await ws.close()
                    await asyncio.gather(*(one(index) for index in range(count)))
            row = dict(kind='notification', mode=mode, added_rtt_ms=rtt,
                       concurrency=concurrency, command_delay_range_ms=[50, max_delay_ms],
                       total_status_gets=sum(query_counts),
                       mean_status_gets=statistics.mean(query_counts),
                       samples_ms=samples, end_to_end_ms=durations, **summary(samples))
            output.append(row)
            print(json.dumps({k:v for k,v in row.items() if k not in ('samples_ms', 'end_to_end_ms')}), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--generated')
    parser.add_argument('--ready')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--events', type=int, default=64)
    parser.add_argument('--event-max-ms', type=int, default=1050)
    parser.add_argument('--event-concurrency', type=int, default=8)
    parser.add_argument('--probe-configured', action='store_true',
                        help='opt in to read-only health sampling of configured services; skip synthetic tests')
    parser.add_argument('--live-samples', type=int, default=20)
    args = parser.parse_args()
    # Ignore host proxy configuration only inside this isolated benchmark process.
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        os.environ.pop(name, None)
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
    if args.server:
        asyncio.run(server(args))
        return
    if not args.output:
        parser.error('--output is required')
    if args.rounds < 0 or args.events < 1 or args.event_max_ms < 50 or args.event_concurrency < 1 or args.live_samples < 1:
        parser.error('invalid sample count, concurrency, rounds or event duration')
    args.output.mkdir(parents=True, exist_ok=True)
    if args.probe_configured:
        probe_configured(args.output, args.rounds, args.live_samples)
        return
    metadata = dict(time=time.strftime('%Y-%m-%dT%H:%M:%S%z'), python=sys.version,
                    platform=platform.platform(), cpu_count=os.cpu_count(),
                    load_average=os.getloadavg(), cpu_affinity_count=len(os.sched_getaffinity(0)),
                    encryption='none; loopback only',
                    delay_model='TCP forwarding proxy delays each direction by added_rtt_ms/2; not tc/netem, no loss or bandwidth cap',
                    packages={name:importlib.metadata.version(name) for name in
                              ('grpcio', 'grpcio-tools', 'protobuf', 'aiohttp', 'httpx', 'requests')})
    with tempfile.TemporaryDirectory(prefix='connection-comm-bench-') as directory:
        generated = Path(directory)
        (generated/'bench.proto').write_text(SCHEMA)
        subprocess.run([sys.executable, '-m', 'grpc_tools.protoc', f'-I{generated}',
                        f'--python_out={generated}', f'--grpc_python_out={generated}',
                        str(generated/'bench.proto')], check=True)
        ready = generated/'ready.json'
        with (args.output/'server.log').open('w') as log:
            proc = subprocess.Popen([sys.executable, __file__, '--server', '--generated',
                                     str(generated), '--ready', str(ready)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic()+15
                while not ready.exists():
                    if proc.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('benchmark server failed; inspect server.log')
                    time.sleep(.05)
                endpoints = json.loads(ready.read_text())
                rows = unary(endpoints, generated, args.rounds)
                (args.output/'unary.json').write_text(json.dumps(rows, indent=2))
                rows += asyncio.run(notifications(endpoints, generated, args.events,
                                                   args.event_max_ms, args.event_concurrency))
                (args.output/'results.json').write_text(json.dumps(dict(metadata=metadata, results=rows), indent=2))
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == '__main__':
    main()
