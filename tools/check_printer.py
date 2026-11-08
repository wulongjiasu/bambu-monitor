#!/usr/bin/env python3
"""快速验证一台 Bambu 打印机的本地 MQTT 凭据。

用法:
    python3 tools/check_printer.py 192.168.1.101 01S00A000000001 12345678

成功时打印 "OK" 并返回 0；失败时打印原因并返回非 0。
"""
import socket
import ssl
import sys
import uuid

import paho.mqtt.client as mqtt

PORT = 8883


def check(ip: str, serial: str, code: str, timeout: float = 8.0) -> int:
    reason: dict[str, str] = {}

    def on_connect(client, userdata, flags, rc, properties=None):
        reason["rc"] = str(rc)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bambu-check-{uuid.uuid4().hex[:8]}",
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )
    client.username_pw_set("bblp", code)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    client.tls_set_context(ctx)
    client.on_connect = on_connect

    try:
        client.connect(ip, PORT, keepalive=30)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  无法连接 {ip}:{PORT} —— {exc}")
        return 2

    client.loop_start()
    try:
        for _ in range(int(timeout * 10)):
            if "rc" in reason:
                break
            import time

            time.sleep(0.1)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    rc = reason.get("rc")
    if rc is None:
        print(f"FAIL  {ip} 无响应（TLS 后未收到 MQTT CONNACK）")
        return 3
    if rc == "Success":
        print(f"OK    {ip}  序列号 {serial}  访问码正确，MQTT 可登录")
        return 0
    print(f"FAIL  {ip}  MQTT 拒绝登录: {rc}")
    if "Not authorized" in rc or "not authorized" in rc:
        print("      原因: 访问码错误，或打印机固件要求开启局域网模式 / 开发者模式")
    return 1


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__.strip())
        return 64
    ip, serial, code = sys.argv[1:4]
    return check(ip, serial, code)


if __name__ == "__main__":
    raise SystemExit(main())
