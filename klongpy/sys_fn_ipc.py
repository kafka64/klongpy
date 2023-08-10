import asyncio
import logging
import pickle
import struct
import sys
import threading
import uuid
from asyncio import StreamReader, StreamWriter
import queue

from klongpy.core import (KGCall, KGFn, KGFnWrapper, KGLambda, KGSym,
                          KlongException, get_fn_arity_str, is_list,
                          reserved_fn_args, reserved_fn_symbol_map)


def encode_message(msg_id, msg):
    data = pickle.dumps(msg)
    length_bytes = struct.pack("!I", len(data))
    return msg_id.bytes + length_bytes + data


def decode_message_len(raw_msglen):
    return struct.unpack('!I', raw_msglen)[0]


def decode_message(raw_msg_id, data):
    msg_id = uuid.UUID(bytes=raw_msg_id)
    message_body = pickle.loads(data)
    return msg_id, message_body


async def stream_send_msg(writer: StreamWriter, msg_id, msg):
    writer.write(encode_message(msg_id, msg))
    await writer.drain()


async def stream_recv_msg(reader: StreamReader):
    raw_msg_id = await reader.readexactly(16)
    raw_msglen = await reader.readexactly(4)
    msglen = decode_message_len(raw_msglen)
    data = await reader.readexactly(msglen)
    return decode_message(raw_msg_id, data)


async def execute_server_command(result_queue, klong, command, nc):
    try:
        klong._context.push({'.clih': nc})
        if isinstance(command, KGRemoteFnCall):
            r = klong[command.sym]
            if callable(r):
                response = r(*command.params)
            else:
                raise KlongException(f"not callable: {command.sym}")
        elif isinstance(command, KGRemoteDictSetCall):
            klong[command.key] = command.value
            response = None
        elif isinstance(command, KGRemoteDictGetCall):
            response = klong[command.key]
            if isinstance(response, KGFnWrapper):
                response = response.fn
        else:
            response = klong(command)
        if isinstance(response, KGFn):
            response = KGRemoteFnRef(response.arity)
    except KeyError as e:
        response = f"symbol not found: {e}"
    except Exception as e:
        response = "internal error"
        logging.error(f"TcpClientHandler::handle_client: Klong error {e}")
        import traceback
        traceback.print_exception(type(e), e, e.__traceback__)
    finally:
        klong._context.pop()
    result_queue.put(response)


async def run_command_on_klongloop(klongloop, klong, command, nc):
    result_queue = queue.Queue()
    klongloop.call_soon_threadsafe(asyncio.create_task, execute_server_command(result_queue, klong, command, nc))
    result = await asyncio.get_event_loop().run_in_executor(None, result_queue.get)
    return result


class NetworkClient(KGLambda):
    def __init__(self, ioloop, klongloop, klong, host, port, max_retries=5, retry_delay=5.0, after_connect=None):
        self.klong = klong
        self.host = host
        self.port = port
        self.pending_responses = {}
        self.running = True
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.klongloop = klongloop
        self.ioloop = ioloop
        self.connect_task = self.ioloop.call_soon_threadsafe(asyncio.create_task, self.connect(after_connect=after_connect))
        self.reader = None
        self.writer = None

    async def _open_connection(self):
        return await asyncio.open_connection(self.host, self.port)

    async def connect(self, after_connect=None):
        current_delay = self.retry_delay
        retries = 0
        while self.running and retries < self.max_retries:
            try:
                logging.info(f"connecting to {self.host}:{self.port}")
                self.reader, self.writer = await self._open_connection()
                logging.info(f"connected to {self.host}:{self.port}")
                retries = 0
                await (after_connect or self.listen)()
            except (OSError, ConnectionResetError, ConnectionRefusedError):
                self._handle_connection_failure()
                self.writer = None
                self.reader = None
                retries += 1
                logging.info(f"connection error to {self.host}:{self.port} retries: {retries} delay: {current_delay}")
                await asyncio.sleep(current_delay)
                current_delay *= 2
                continue
        if retries >= self.max_retries:
            logging.info(f"Max retries reached: {self.max_retries} {self.host}:{self.port}")
        logging.info(f"Stopping client: {self.host}:{self.port}")

    def _handle_connection_failure(self):
        failure_result = KlongException(f"connection lost: {self.host}:{self.port}")
        for future in self.pending_responses.values():
            future.set_exception(failure_result)
        self.pending_responses.clear()

    async def _run_server_command(self, msg):
        return await run_command_on_klongloop(self.klongloop, self.klong, msg, self)

    async def listen(self):
        while self.running:
            msg_id, msg = await stream_recv_msg(self.reader)
            if msg_id in self.pending_responses:
                future = self.pending_responses.pop(msg_id)
                future.set_result(msg)
                continue
            response = await self._run_server_command(msg)
            await stream_send_msg(self.writer, msg_id, response)

    def call(self, msg):
        if self.writer is None:
            raise KlongException(f"connection not established: {self.host}:{self.port}")

        msg_id = uuid.uuid4()
        future = self.ioloop.create_future()
        self.pending_responses[msg_id] = future

        async def send_message_and_get_result():
            await stream_send_msg(self.writer, msg_id, msg)
            return await future
        
        fut = asyncio.run_coroutine_threadsafe(send_message_and_get_result(), self.ioloop)

        return fut.result()

    def __call__(self, _, ctx):
        x = ctx[reserved_fn_symbol_map[reserved_fn_args[0]]]
        try:
            msg = KGRemoteFnCall(x[0], x[1:]) if is_list(x) and len(x) > 0 and isinstance(x[0],KGSym) else x
            response = self.call(msg)
            if isinstance(x,KGSym) and isinstance(response, KGRemoteFnRef):
                response = KGRemoteFnProxy(self.nc, x, response.arity)
            return response
        except Exception as e:
            import traceback
            traceback.print_exception(type(e), e, e.__traceback__)
            raise e

    async def _close_async(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

    def close(self):
        self.running = False
        self.connect_task.cancel()
        fut = asyncio.run_coroutine_threadsafe(self._close_async(), self.ioloop)
        fut.result()

    def is_open(self):
        return self.writer is not None and not self.writer.is_closing()

    def get_arity(self):
        return 1
        
    def __str__(self):
        return f"remote[{self.host}:{self.port}]:fn"

    @staticmethod
    def create(ioloop, klongloop, klong, host, port):
        return NetworkClient(ioloop, klongloop, klong, host, port)

    @staticmethod
    def create_from_addr(ioloop, klongloop, klong, addr):
        addr = str(addr)
        parts = addr.split(":")
        host = parts[0] if len(parts) > 1 else "localhost"
        port = int(parts[0] if len(parts) == 1 else parts[1])
        return NetworkClient.create(ioloop, klongloop, klong, host, port)


class KGRemoteFnRef:
    def __init__(self, arity):
        self.arity = arity

    def __str__(self):
        return get_fn_arity_str(self.arity)


class KGRemoteFnCall:
    def __init__(self, sym: KGSym, params):
        self.sym = sym
        self.params = params


class KGRemoteDictSetCall:
    def __init__(self, key, value):
        self.key = key
        self.value = value


class KGRemoteDictGetCall:
    def __init__(self, key):
        self.key = key


class KGRemoteFnProxy(KGLambda):

    def __init__(self, nc: NetworkClient, sym: KGSym, arity):
        self.nc = nc
        self.sym = sym
        self.args = reserved_fn_args[:arity]

    def __call__(self, _, ctx):
        params = [ctx[reserved_fn_symbol_map[x]] for x in reserved_fn_args[:len(self.args)]]
        return self.nc.call(KGRemoteFnCall(self.sym, params))

    def __str__(self):
        return f"{self.nc.__str__()}:{self.sym}{super().__str__()}"


class TcpClientHandler:
    def __init__(self, klongloop, klong):
        self.klong = klong
        self.klongloop = klongloop

    async def handle_client(self, reader: StreamReader, writer: StreamWriter):
        while True:
            try:
                msg_id, command = await stream_recv_msg(reader)
                if not command:
                    break
            except EOFError as e:
                break
            response = await run_command_on_klongloop(self.klongloop, self.klong, command, None)
            await stream_send_msg(writer, msg_id, response)
        writer.close()
        await writer.wait_closed()


class TcpServerHandler:
    def __init__(self):
        self.client_handler = None
        self.task = None
        self.server = None
        self.connections = []

    def create_server(self, ioloop, klongloop, klong, bind, port):
        if self.task is not None:
            return 0
        self.client_handler = TcpClientHandler(klongloop, klong)
        self.task = ioloop.call_soon_threadsafe(asyncio.create_task, self.tcp_producer(bind, port))
        return 1

    def shutdown_server(self):
        if self.task is None:
            return 0
        for writer in self.connections:
            if not writer.is_closing():
                writer.close()
        self.connections.clear()

        self.server.close()
        self.server = None
        self.task.cancel()
        self.task = None
        self.client_handler = None
        return 1

    async def handle_client(self, reader, writer):
        self.connections.append(writer)

        try:
            await self.client_handler.handle_client(reader, writer)
        finally:
            if not writer.is_closing():
                writer.close()
            await writer.wait_closed()
            if writer in self.connections:
                self.connections.remove(writer)

    async def tcp_producer(self, bind, port):
        self.server = await asyncio.start_server(self.handle_client, bind, port, reuse_address=True)

        addr = self.server.sockets[0].getsockname()
        logging.info(f'Serving on {addr}')

        async with self.server:
            await self.server.serve_forever()


class NetworkClientDictHandle(dict):
    def __init__(self, nc: NetworkClient):
        self.nc = nc

    def __getitem__(self, x):
        return self.get(x)

    def __setitem__(self, x, y):
        return self.set(x, y)

    def __contains__(self, x):
        raise NotImplementedError()

    def get(self, x):
        try:
            response = self.nc.call(KGRemoteDictGetCall(x))
            if isinstance(x,KGSym) and isinstance(response, KGRemoteFnRef):
                response = KGRemoteFnProxy(self.nc, x, response.arity)
            return response
        except Exception as e:
            import traceback
            traceback.print_exception(type(e), e, e.__traceback__)
            raise e

    def set(self, x, y):
        try:
            self.nc.call(KGRemoteDictSetCall(x, y))
            return self
        except Exception as e:
            import traceback
            traceback.print_exception(type(e), e, e.__traceback__)
            raise e

    def close(self):
        return self.nc.close()

    def is_open(self):
        return self.nc.is_open()
    
    def __str__(self):
        return f"remote[{self.nc.host}:{self.nc.port}]:dict"


def eval_sys_fn_create_client(klong, x):
    """

        .cli(x)                                      [Create-IPC-client]

        Return a function which evaluates commands on a remote KlongPy server.

        If "x" is an integer, then it is interpreted as a port in "localhost:<port>".
        if "x" is a string, then it is interpreted as a host address "<host>:<port>"

        If "x" is a remote dictionary, the underlying network connection 
        is shared and a remote function is returned.
        
        Connection examples:  
      
                   .cli(8888)            --> remote function to localhost:8888
                   .cli("localhost:8888") --> remote function to localhost:8888
                   
                   d::.clid(8888)
                   .cli(d)                --> remote function to same connection as d

        Evaluation examples:

                   f::.cli(8888)

            A string is passed it is evaluated remotely:

                   f("hello")             --> "hello" is evaluated remotely
                   f("avg::{(+/x)%#x}")   --> "avg" function is defined remotely
                   f("avg(!100)")         --> 49.5 (computed remotely)

            Remote functions may be evaluated by passing an array with the first element 
            being the symbol of the remote function to execute.  The remaining elements
            are supplied as parameters:

            Example: call :avg with a locally generated !100 range which is passed to the remote server.

                   f(:avg,,!100)          --> 49.5
                
            Similary:

                   b::!100
                   f(:avg,,b)             --> 49.5
        
            When a symbol is applied, the remote value is returned.  
            For functions, a remote function proxy is returned.

            Example: retrieve a function proxy to :avg and then call it as if it were a local function.

                   q::f(:avg)
                   q(!100)                --> 49.5

            Example: retrieve a copy of a remote array.

                   f("b::!100")
                   p::f(:b)               --> "p: now holds a copy of the remote array "b"
                   
    """
    x = x.a if isinstance(x,KGCall) else x
    if isinstance(x,NetworkClient):
        return x
    system = klong['.system']
    ioloop = system['ioloop']
    klongloop = system['klongloop']
    nc = x.nc if isinstance(x,NetworkClientDictHandle) else NetworkClient.create_from_addr(ioloop, klongloop, klong, x)
    return nc


def eval_sys_fn_create_dict_client(klong, x):
    """

        .clid(x)                                [Create-IPC-dict-client]

        Return a dictionary which evaluates set/get operations on a remote KlongPy server.

        If "x" is an integer, then it is interpreted as a port in "localhost:<port>".
        if "x" is a string, then it is interpreted as a host address "<host>:<port>"

        If "x" is a remote function, the underlying network connection 
        is shared and a remote function is returned.
        
        Examples:  .cli(8888)             --> remote function to localhost:8888
                   .cli("localhost:8888") --> remote function to localhost:8888
                   .cli(d)                --> remote function to same connection as d
        
        Connection examples:  
      
                   .cli(8888)            --> remote function to localhost:8888
                   .cli("localhost:8888") --> remote function to localhost:8888
                   
                   f::.cli(8888)
                   .cli(f)                --> remote function to same connection as f

        Evaluation examples:

                   d::.cli(8888)

            Set a remote key/value pair :foo -> 2 on the remote server.

                   d,[:foo 2]             --> sets :foo to 2
                   d,[:bar "hello"]       --> sets :bar to "hello"
                   d,:fn,{x+1}            --> sets :fn to the monad {x+1)

            Get a remote value:

                   d?:foo                 --> 2
                   d?:bar                 --> hello
                   d?:fn                 --> remote function proxy to :avg

            To use the remote function proxy:
                   q::d?:fn
                   q(2)               --> 3 (remotely executed after passing 2)

    """
    x = x.a if isinstance(x,KGCall) else x
    if isinstance(x,NetworkClientDictHandle):
        return x
    system = klong['.system']
    ioloop = system['ioloop']
    klongloop = system['klongloop']
    nc = x.nc if isinstance(x,NetworkClient) else NetworkClient.create_from_addr(ioloop, klongloop, klong, x)
    return NetworkClientDictHandle(nc)


def eval_sys_fn_shutdown_client(x):
    """

        .clic(x)                                      [Close-IPC-client]

        Close a remote dictionary or function opened by .cli or .clid.

        Returns 1 if closed, 0 if already closed.

        When a connection is closed, all remote proxies / functions tied to this connection
        will also close and will fail if called.

    """
    if isinstance(x, KGCall) and issubclass(type(x.a), KGLambda):
        x = x.a
        if isinstance(x, (NetworkClient, NetworkClientDictHandle)) and x.is_open():
            x.close()
            return 1
    return 0


_ipc_tcp_server = TcpServerHandler()


def eval_sys_fn_create_ipc_server(klong, x):
    """

        .srv(x)                                       [Start-IPC-server]

        Open a server port to accept IPC connections.

        If "x" is an integer, then it is interpreted as a port in "<all>:<port>".
        if "x" is a string, then it is interpreted as a bind address "<bind>:<port>"

        if "x" is 0, then the server is closed and existing client connections are dropped.

    """
    global _ipc_tcp_server
    x = str(x)
    parts = x.split(":")
    bind = parts[0] if len(parts) > 1 else None
    port = int(parts[0] if len(parts) == 1 else parts[1])
    if len(parts) == 1 and port == 0:
        return _ipc_tcp_server.shutdown_server()
    system = klong['.system']
    ioloop = system['ioloop']
    klongloop = system['klongloop']
    return _ipc_tcp_server.create_server(ioloop, klongloop, klong, bind, port)


class KGAsyncCall(KGLambda):
    def __init__(self, klongloop, fn, cb):
        self.klongloop = klongloop
        self.cb = cb
        self.fn = fn
        self.args = [reserved_fn_symbol_map[x] for x in reserved_fn_args[:fn.arity]]
   
    async def acall(self, klong, params):
        r = klong.call(KGCall(self.fn.a, [*params], self.fn.arity))
        self.cb(r)

    def __call__(self, klong, ctx):
        params = [ctx[x] for x in self.args]
        self.klongloop.create_task(self.acall(klong, params))
        return 1

    def __str__(self):
        return f"async:{super().__str__()}"


def eval_sys_fn_create_async_wrapper(klong, x, y):
    """

        .async(x,y)                             [Async-function-wrapper]

        Returns an async functional wrapper for the function "x" and calls "y"
        when completed. The wrapper has the same arity as the wrapped function.

    """
    if not issubclass(type(x),KGFn):
        raise KlongException("x must be a function")
    if not issubclass(type(y),KGFn):
        raise KlongException("y must be a function")
    system = klong['.system']
    klongloop = system['klongloop']
    return KGAsyncCall(klongloop, x, KGFnWrapper(klong, y))


def create_system_functions_ipc():
    def _get_name(s):
        i = s.index(".")
        return s[i : i + s[i:].index("(")]

    registry = {}

    m = sys.modules[__name__]
    for x in filter(lambda n: n.startswith("eval_sys_"), dir(m)):
        fn = getattr(m, x)
        registry[_get_name(fn.__doc__)] = fn

    return registry
