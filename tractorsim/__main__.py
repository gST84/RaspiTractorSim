"""python3 -m tractorsim [--bus can0] [--port 8080]"""
import argparse
import asyncio
import logging
import os
import signal

from .app import App

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(prog="tractorsim", description="ISOBUS-tractorsimulator met virtuele terminal")
    ap.add_argument("--bus", default=os.environ.get("TRACTORSIM_BUS", "can0"),
                    help="can0 / vcan0 (SocketCAN), virtual (UDP-bus om te testen zonder hardware), virtual:HOST:POORT, "
                         "of pythoncan:INTERFACE:KANAAL (python-can, bv. pythoncan:pcan:PCAN_USBBUS1, pythoncan:kvaser:0, "
                         "pythoncan:vector:0, pythoncan:ixxat:0)")
    ap.add_argument("--bitrate", type=int, default=250000,
                    help="voor de busbelasting en voor python-can; stel de bitrate van SocketCAN in met ip link")
    ap.add_argument("--listen", action="store_true", default=os.environ.get("TRACTORSIM_LISTEN") == "1",
                    help="luistermodus: geen tractor-ECU, VT of GNSS en niets zenden (enkel de pagina Variabelen)")
    ap.add_argument("--host", default="0.0.0.0", help="adres van de webinterface (standaard: alle netwerkkaarten)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TRACTORSIM_PORT") or os.environ.get("PORT") or 8080))
    ap.add_argument("--data", default=os.environ.get("TRACTORSIM_DATA", os.path.expanduser("~/.tractorsim")),
                    help="map voor instellingen en opgeslagen object pools")
    ap.add_argument("--web", default=os.path.join(os.path.dirname(HERE), "web"), help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = App(a.bus, a.data, a.web, a.host, a.port, a.bitrate, listen=a.listen)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async def run():
        await app.start()
        await stop.wait()
        try:
            app._save()
        except OSError:
            pass

    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        pass
    finally:
        app.bus.close()


if __name__ == "__main__":
    main()
