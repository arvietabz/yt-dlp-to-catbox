import asyncio
import ipaddress
from config import (
    IS_TAILSCALE_PROXY,
    LOCAL_BRIDGE_PORT,
    REMOTE_PROXY_HOST,
    REMOTE_PROXY_PORT,
    TS_SOCKS_HOST,
    TS_SOCKS_PORT,
)

async def handle_bridge_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        ts_reader, ts_writer = await asyncio.open_connection(TS_SOCKS_HOST, TS_SOCKS_PORT)
        
        ts_writer.write(b"\x05\x01\x00")
        await ts_writer.drain()
        socks_init = await ts_reader.readexactly(2)
        if socks_init != b"\x05\x00":
            writer.close()
            return

        try:
            ip_obj = ipaddress.ip_address(REMOTE_PROXY_HOST)
            if ip_obj.version == 4:
                req = b"\x05\x01\x00\x01" + ip_obj.packed + REMOTE_PROXY_PORT.to_bytes(2, "big")
            else:
                req = b"\x05\x01\x00\x04" + ip_obj.packed + REMOTE_PROXY_PORT.to_bytes(2, "big")
        except ValueError:
            host_bytes = REMOTE_PROXY_HOST.encode("utf-8")
            req = b"\x05\x01\x00\x03" + len(host_bytes).to_bytes(1, "big") + host_bytes + REMOTE_PROXY_PORT.to_bytes(2, "big")
            
        ts_writer.write(req)
        await ts_writer.drain()
        
        resp = await ts_reader.read(10)
        if len(resp) < 2 or resp[1] != 0x00:
            writer.close()
            return

        async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter):
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                try:
                    w.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe(reader, ts_writer),
            pipe(ts_reader, writer),
            return_exceptions=True
        )

    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

async def start_proxy_bridge():
    if IS_TAILSCALE_PROXY and REMOTE_PROXY_HOST:
        server = await asyncio.start_server(
            handle_bridge_client,
            "127.0.0.1",
            LOCAL_BRIDGE_PORT
        )
        print(f"Proxy bridge running on 127.0.0.1:{LOCAL_BRIDGE_PORT} -> Tailscale SOCKS5 -> {REMOTE_PROXY_HOST}:{REMOTE_PROXY_PORT}")
        return server
    return None
