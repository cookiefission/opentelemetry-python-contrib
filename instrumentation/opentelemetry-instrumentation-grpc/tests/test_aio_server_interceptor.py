# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint:disable=unused-argument
# pylint:disable=no-self-use
import asyncio
import grpc
import grpc.aio
from concurrent.futures.thread import ThreadPoolExecutor

from time import sleep
from opentelemetry.test.test_base import TestBase
from opentelemetry import trace
import opentelemetry.instrumentation.grpc
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.trace import StatusCode

from .protobuf.test_server_pb2 import Request, Response
from .protobuf.test_server_pb2_grpc import (
    GRPCTestServerServicer,
    add_GRPCTestServerServicer_to_server,
)
from opentelemetry.instrumentation.grpc import (
    GrpcAioInstrumentorServer,
    aio_server_interceptor,
)


class Servicer(GRPCTestServerServicer):
    """Our test servicer"""

    # pylint:disable=C0103
    async def SimpleMethod(self, request, context):
        return Response(
            server_id=request.client_id,
            response_data=request.request_data,
        )

    # pylint:disable=C0103
    async def ServerStreamingMethod(self, request, context):
        for data in ("one", "two", "three"):
            yield Response(
                server_id=request.client_id,
                response_data=data,
            )


def run_with_test_server(runnable, servicer=Servicer(), add_interceptor=True):
    if add_interceptor:
        interceptors = [aio_server_interceptor()]
        server = grpc.aio.server(interceptors=interceptors)
    else:
        server = grpc.aio.server()

    add_GRPCTestServerServicer_to_server(servicer, server)

    port = server.add_insecure_port("[::]:0")
    channel = grpc.aio.insecure_channel(f"localhost:{port:d}")

    async def do_request():
        await server.start()
        resp = await runnable(channel)
        await server.stop(1000)
        return resp

    loop = asyncio.get_event_loop_policy().get_event_loop()
    return loop.run_until_complete(do_request())


class TestOpenTelemetryAioServerInterceptor(TestBase):
    def test_instrumentor(self):
        """Check that automatic instrumentation configures the interceptor"""
        rpc_call = "/GRPCTestServer/SimpleMethod"

        grpc_aio_server_instrumentor = GrpcAioInstrumentorServer()
        grpc_aio_server_instrumentor.instrument()

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            return await channel.unary_unary(rpc_call)(msg)

        run_with_test_server(request, add_interceptor=False)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertEqual(span.name, rpc_call)
        self.assertIs(span.kind, trace.SpanKind.SERVER)

        # Check version and name in span's instrumentation info
        self.assertEqualSpanInstrumentationInfo(
            span, opentelemetry.instrumentation.grpc
        )

        # Check attributes
        self.assertSpanHasAttributes(
            span,
            {
                SpanAttributes.NET_PEER_IP: "[::1]",
                SpanAttributes.NET_PEER_NAME: "localhost",
                SpanAttributes.RPC_METHOD: "SimpleMethod",
                SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                SpanAttributes.RPC_SYSTEM: "grpc",
                SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.OK.value[
                    0
                ],
            },
        )

        grpc_aio_server_instrumentor.uninstrument()


    def test_uninstrument(self):
        """Check that uninstrument removes the interceptor"""
        rpc_call = "/GRPCTestServer/SimpleMethod"

        grpc_aio_server_instrumentor = GrpcAioInstrumentorServer()
        grpc_aio_server_instrumentor.instrument()
        grpc_aio_server_instrumentor.uninstrument()

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            return await channel.unary_unary(rpc_call)(msg)

        run_with_test_server(request, add_interceptor=False)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 0)


    def test_create_span(self):
        """Check that the interceptor wraps calls with spans server-side."""
        rpc_call = "/GRPCTestServer/SimpleMethod"

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            return await channel.unary_unary(rpc_call)(msg)

        run_with_test_server(request)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertEqual(span.name, rpc_call)
        self.assertIs(span.kind, trace.SpanKind.SERVER)

        # Check version and name in span's instrumentation info
        self.assertEqualSpanInstrumentationInfo(
            span, opentelemetry.instrumentation.grpc
        )

        # Check attributes
        self.assertSpanHasAttributes(
            span,
            {
                SpanAttributes.NET_PEER_IP: "[::1]",
                SpanAttributes.NET_PEER_NAME: "localhost",
                SpanAttributes.RPC_METHOD: "SimpleMethod",
                SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                SpanAttributes.RPC_SYSTEM: "grpc",
                SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.OK.value[
                    0
                ],
            },
        )

    def test_create_two_spans(self):
        """Verify that the interceptor captures sub spans within the given
        trace"""
        rpc_call = "/GRPCTestServer/SimpleMethod"

        class TwoSpanServicer(GRPCTestServerServicer):
            # pylint:disable=C0103
            async def SimpleMethod(self, request, context):

                # create another span
                tracer = trace.get_tracer(__name__)
                with tracer.start_as_current_span("child") as child:
                    child.add_event("child event")

                return Response(
                    server_id=request.client_id,
                    response_data=request.request_data,
                )

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            return await channel.unary_unary(rpc_call)(msg)

        run_with_test_server(request, servicer=TwoSpanServicer())

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 2)
        child_span = spans_list[0]
        parent_span = spans_list[1]

        self.assertEqual(parent_span.name, rpc_call)
        self.assertIs(parent_span.kind, trace.SpanKind.SERVER)

        # Check version and name in span's instrumentation info
        self.assertEqualSpanInstrumentationInfo(
            parent_span, opentelemetry.instrumentation.grpc
        )

        # Check attributes
        self.assertSpanHasAttributes(
            parent_span,
            {
                SpanAttributes.NET_PEER_IP: "[::1]",
                SpanAttributes.NET_PEER_NAME: "localhost",
                SpanAttributes.RPC_METHOD: "SimpleMethod",
                SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                SpanAttributes.RPC_SYSTEM: "grpc",
                SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.OK.value[
                    0
                ],
            },
        )

        # Check the child span
        self.assertEqual(child_span.name, "child")
        self.assertEqual(
            parent_span.context.trace_id, child_span.context.trace_id
        )

    def test_create_span_streaming(self):
        """Check that the interceptor wraps calls with spans server-side, on a
        streaming call."""
        rpc_call = "/GRPCTestServer/ServerStreamingMethod"

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            async for response in channel.unary_stream(rpc_call)(msg):
                print(response)

        run_with_test_server(request)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertEqual(span.name, rpc_call)
        self.assertIs(span.kind, trace.SpanKind.SERVER)

        # Check version and name in span's instrumentation info
        self.assertEqualSpanInstrumentationInfo(
            span, opentelemetry.instrumentation.grpc
        )

        # Check attributes
        self.assertSpanHasAttributes(
            span,
            {
                SpanAttributes.NET_PEER_IP: "[::1]",
                SpanAttributes.NET_PEER_NAME: "localhost",
                SpanAttributes.RPC_METHOD: "ServerStreamingMethod",
                SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                SpanAttributes.RPC_SYSTEM: "grpc",
                SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.OK.value[
                    0
                ],
            },
        )

    def test_create_two_spans_streaming(self):
        """Verify that the interceptor captures sub spans within the given
        trace"""
        rpc_call = "/GRPCTestServer/ServerStreamingMethod"

        class TwoSpanServicer(GRPCTestServerServicer):
            # pylint:disable=C0103
            async def ServerStreamingMethod(self, request, context):
                # create another span
                tracer = trace.get_tracer(__name__)
                with tracer.start_as_current_span("child") as child:
                    child.add_event("child event")

                for data in ("one", "two", "three"):
                    yield Response(
                        server_id=request.client_id,
                        response_data=data,
                    )

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            async for response in channel.unary_stream(rpc_call)(msg):
                print(response)

        run_with_test_server(request, servicer=TwoSpanServicer())

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 2)
        child_span = spans_list[0]
        parent_span = spans_list[1]

        self.assertEqual(parent_span.name, rpc_call)
        self.assertIs(parent_span.kind, trace.SpanKind.SERVER)

        # Check version and name in span's instrumentation info
        self.assertEqualSpanInstrumentationInfo(
            parent_span, opentelemetry.instrumentation.grpc
        )

        # Check attributes
        self.assertSpanHasAttributes(
            parent_span,
            {
                SpanAttributes.NET_PEER_IP: "[::1]",
                SpanAttributes.NET_PEER_NAME: "localhost",
                SpanAttributes.RPC_METHOD: "ServerStreamingMethod",
                SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                SpanAttributes.RPC_SYSTEM: "grpc",
                SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.OK.value[
                    0
                ],
            },
        )

        # Check the child span
        self.assertEqual(child_span.name, "child")
        self.assertEqual(
            parent_span.context.trace_id, child_span.context.trace_id
        )

    def test_span_lifetime(self):
        """Verify that the interceptor captures sub spans within the given
        trace"""
        rpc_call = "/GRPCTestServer/SimpleMethod"

        class SpanLifetimeServicer(GRPCTestServerServicer):
            # pylint:disable=C0103
            async def SimpleMethod(self, request, context):
                self.span = trace.get_current_span()

                return Response(
                    server_id=request.client_id,
                    response_data=request.request_data,
                )

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            return await channel.unary_unary(rpc_call)(msg)

        lifetime_servicer = SpanLifetimeServicer()
        active_span_before_call = trace.get_current_span()

        run_with_test_server(request, servicer=lifetime_servicer)

        active_span_in_handler = lifetime_servicer.span
        active_span_after_call = trace.get_current_span()

        self.assertEqual(active_span_before_call, trace.INVALID_SPAN)
        self.assertEqual(active_span_after_call, trace.INVALID_SPAN)
        self.assertIsInstance(active_span_in_handler, trace_sdk.Span)
        self.assertIsNone(active_span_in_handler.parent)

    def test_sequential_server_spans(self):
        """Check that sequential RPCs get separate server spans."""
        rpc_call = "/GRPCTestServer/SimpleMethod"

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            return await channel.unary_unary(rpc_call)(msg)

        async def sequential_requests(channel):
            await request(channel)
            await request(channel)

        run_with_test_server(sequential_requests)

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 2)

        span1, span2 = spans_list
        # Spans should belong to separate traces
        self.assertNotEqual(span1.context.span_id, span2.context.span_id)
        self.assertNotEqual(span1.context.trace_id, span2.context.trace_id)

        for span in (span1, span2):
            # each should be a root span
            self.assertIsNone(span2.parent)

            # check attributes
            self.assertSpanHasAttributes(
                span,
                {
                    SpanAttributes.NET_PEER_IP: "[::1]",
                    SpanAttributes.NET_PEER_NAME: "localhost",
                    SpanAttributes.RPC_METHOD: "SimpleMethod",
                    SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                    SpanAttributes.RPC_SYSTEM: "grpc",
                    SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.OK.value[
                        0
                    ],
                },
            )

    def test_concurrent_server_spans(self):
        """Check that concurrent RPC calls don't interfere with each other.

        This is the same check as test_sequential_server_spans except that the
        RPCs are concurrent. Two handlers are invoked at the same time on two
        separate threads. Each one should see a different active span and
        context.
        """
        rpc_call = "/GRPCTestServer/SimpleMethod"
        latch = get_latch(2)

        class LatchedServicer(GRPCTestServerServicer):
            # pylint:disable=C0103
            async def SimpleMethod(self, request, context):
                await latch()
                return Response(
                    server_id=request.client_id,
                    response_data=request.request_data,
                )

        async def request(channel):
            request = Request(client_id=1, request_data="test")
            msg = request.SerializeToString()
            return await channel.unary_unary(rpc_call)(msg)

        async def concurrent_requests(channel):
            await asyncio.gather(request(channel), request(channel))

        run_with_test_server(concurrent_requests, servicer=LatchedServicer())

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 2)

        span1, span2 = spans_list
        # Spans should belong to separate traces
        self.assertNotEqual(span1.context.span_id, span2.context.span_id)
        self.assertNotEqual(span1.context.trace_id, span2.context.trace_id)

        for span in (span1, span2):
            # each should be a root span
            self.assertIsNone(span2.parent)

            # check attributes
            self.assertSpanHasAttributes(
                span,
                {
                    SpanAttributes.NET_PEER_IP: "[::1]",
                    SpanAttributes.NET_PEER_NAME: "localhost",
                    SpanAttributes.RPC_METHOD: "SimpleMethod",
                    SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                    SpanAttributes.RPC_SYSTEM: "grpc",
                    SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.OK.value[
                        0
                    ],
                },
            )

    def test_abort(self):
        """Check that we can catch an abort properly"""
        rpc_call = "/GRPCTestServer/SimpleMethod"
        failure_message = "failure message"

        class AbortServicer(GRPCTestServerServicer):
            # pylint:disable=C0103
            async def SimpleMethod(self, request, context):
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION, failure_message
                )

        testcase = self

        async def request(channel):
            request = Request(client_id=1, request_data=failure_message)
            msg = request.SerializeToString()

            with testcase.assertRaises(Exception):
                await channel.unary_unary(rpc_call)(msg)

        run_with_test_server(request, servicer=AbortServicer())

        spans_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(spans_list), 1)
        child_span = spans_list[0]

        self.assertEqual(len(spans_list), 1)
        span = spans_list[0]

        self.assertEqual(span.name, rpc_call)
        self.assertIs(span.kind, trace.SpanKind.SERVER)

        # Check version and name in span's instrumentation info
        self.assertEqualSpanInstrumentationInfo(
            span, opentelemetry.instrumentation.grpc
        )

        # make sure this span errored, with the right status and detail
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(
            span.status.description,
            f"{grpc.StatusCode.FAILED_PRECONDITION}:{failure_message}",
        )

        # Check attributes
        self.assertSpanHasAttributes(
            span,
            {
                SpanAttributes.NET_PEER_IP: "[::1]",
                SpanAttributes.NET_PEER_NAME: "localhost",
                SpanAttributes.RPC_METHOD: "SimpleMethod",
                SpanAttributes.RPC_SERVICE: "GRPCTestServer",
                SpanAttributes.RPC_SYSTEM: "grpc",
                SpanAttributes.RPC_GRPC_STATUS_CODE: grpc.StatusCode.FAILED_PRECONDITION.value[
                    0
                ],
            },
        )


def get_latch(num):
    """Get a countdown latch function for use in n threads."""
    cv = asyncio.Condition()
    count = 0

    async def countdown_latch():
        """Block until n-1 other threads have called."""
        nonlocal count
        async with cv:
            count += 1
            cv.notify()

        async with cv:
            while count < num:
                await cv.wait()

    return countdown_latch
