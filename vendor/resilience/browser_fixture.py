import asyncio
import recovery_gateway as gateway


down = True
running = False


async def main():
    async def upstream(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = b"<html><head></head><body><h1>Dex de teste</h1></body></html>"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                     + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    original_connect = asyncio.open_connection

    async def connect(host, port):
        assert port == 8787
        return await original_connect(host, source.sockets[0].getsockname()[1])

    async def health(port):
        return not down

    async def broker():
        return True

    async def repair():
        global down, running
        await asyncio.sleep(8)
        down = running = False

    async def command(*args):
        global running
        if args[0] == "start" and not running:
            running = True
            asyncio.create_task(repair())
        return 0, "active" if running else "inactive"

    def authorize(headers):
        if headers.get("cookie") != "fixture=authorized":
            raise gateway.AccessDenied(403)
        return ""

    gateway.health = health
    gateway.broker_ready = broker
    gateway.command = command
    gateway.authorize = authorize
    gateway.asyncio.open_connection = connect
    source = await asyncio.start_server(upstream, "127.0.0.1", 0)
    server = await asyncio.start_server(gateway.client, "127.0.0.1", 0)
    gateway.ORIGIN = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    print(gateway.ORIGIN, flush=True)
    async with server, source:
        await server.serve_forever()


asyncio.run(main())
