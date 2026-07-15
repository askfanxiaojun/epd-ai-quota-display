#!/usr/bin/env python3
"""Install an nRF5 SDK Secure DFU package using the computer's BLE adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from bleak import BleakClient, BleakScanner


DFU_CONTROL_UUID = "8ec90001-f315-4f60-9fb8-838830daea50"
DFU_PACKET_UUID = "8ec90002-f315-4f60-9fb8-838830daea50"
DFU_BUTTONLESS_UUID = "8ec90003-f315-4f60-9fb8-838830daea50"

OP_CREATE = 0x01
OP_SET_PRN = 0x02
OP_CRC = 0x03
OP_EXECUTE = 0x04
OP_SELECT = 0x06
OP_RESPONSE = 0x60

OBJECT_COMMAND = 0x01
OBJECT_DATA = 0x02

RESULTS = {
    0x00: "invalid code",
    0x01: "success",
    0x02: "opcode not supported",
    0x03: "invalid parameter",
    0x04: "insufficient resources",
    0x05: "invalid object",
    0x07: "unsupported type",
    0x08: "operation not permitted",
    0x0A: "operation failed",
    0x0B: "extended error",
}


@dataclass(frozen=True)
class DFUPackage:
    init_packet: bytes
    firmware: bytes
    version: int | None


def load_package(path: Path) -> DFUPackage:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        application = manifest["manifest"]["application"]
        init_packet = archive.read(application["dat_file"])
        firmware = archive.read(application["bin_file"])
        version = application.get("init_packet_data", {}).get("application_version")
    return DFUPackage(init_packet, firmware, version)


async def find_device_or_bootloader(
    name_prefix: str,
    timeout: float = 30.0,
    known_address: str | None = None,
):
    print(f"持续扫描 {name_prefix}* 或 DfuTarg（最长 {timeout:.0f} 秒）…")
    found = asyncio.Future()

    def on_advertisement(device, advertisement) -> None:
        name = advertisement.local_name or device.name or ""
        services = {item.lower() for item in advertisement.service_uuids}
        advertises_dfu = any(item == "fe59" or item.startswith("0000fe59") for item in services)
        is_known_app = known_address is not None and device.address.upper() == known_address.upper()
        is_app = name.startswith(name_prefix) or is_known_app
        is_bootloader = "dfu" in name.lower() or advertises_dfu
        if not found.done() and (is_app or is_bootloader):
            found.set_result((device, advertisement.rssi, is_bootloader, name))

    async with BleakScanner(on_advertisement):
        try:
            device, rssi, is_bootloader, name = await asyncio.wait_for(found, timeout=timeout)
        except TimeoutError as exc:
            raise RuntimeError(f"{timeout:.0f} 秒内没有发现 {name_prefix}* 或 DfuTarg") from exc
    print(f"发现 {name} ({device.address}, RSSI {rssi} dBm)")
    return device, is_bootloader


async def enter_bootloader(device) -> None:
    disconnected = asyncio.Event()

    def on_disconnect(_client) -> None:
        disconnected.set()

    client = BleakClient(device, disconnected_callback=on_disconnect)
    await client.connect()
    try:
        characteristic = client.services.get_characteristic(DFU_BUTTONLESS_UUID)
        if characteristic is None:
            if client.services.get_characteristic(DFU_CONTROL_UUID):
                return
            raise RuntimeError("设备没有 Buttonless DFU 特征")

        indication = asyncio.Event()

        def on_indication(_sender, data: bytearray) -> None:
            print(f"设备确认进入 DFU：{bytes(data).hex()}")
            indication.set()

        await client.start_notify(characteristic, on_indication)
        print("正在让设备重启到固件升级模式 …")
        try:
            await client.write_gatt_char(characteristic, b"\x01", response=True)
        except Exception:
            if not disconnected.is_set():
                raise
        try:
            await asyncio.wait_for(indication.wait(), timeout=3.0)
        except TimeoutError:
            pass
        try:
            await asyncio.wait_for(disconnected.wait(), timeout=8.0)
        except TimeoutError:
            pass
    finally:
        if client.is_connected:
            await client.disconnect()


async def find_bootloader(timeout: float = 30.0, *, exclude_address: str | None = None):
    print("等待 DFU 启动器广播 …")
    found = asyncio.Future()

    def on_advertisement(device, advertisement) -> None:
        name = advertisement.local_name or device.name or ""
        services = {item.lower() for item in advertisement.service_uuids}
        advertises_dfu = any(item == "fe59" or item.startswith("0000fe59") for item in services)
        is_new_device = exclude_address is None or device.address != exclude_address
        if not found.done() and is_new_device and ("dfu" in name.lower() or advertises_dfu):
            found.set_result((device, name))

    async with BleakScanner(on_advertisement):
        try:
            device, name = await asyncio.wait_for(found, timeout=timeout)
        except TimeoutError as exc:
            raise RuntimeError(f"{timeout:.0f} 秒内没有找到 DfuTarg") from exc
    print(f"找到 DFU 启动器：{name or device.name or '未命名'} ({device.address})")
    return device


class SecureDFU:
    def __init__(self, client: BleakClient):
        self.client = client
        self.responses: asyncio.Queue[bytes] = asyncio.Queue()
        packet = client.services.get_characteristic(DFU_PACKET_UUID)
        if packet is None:
            raise RuntimeError("DFU Packet 特征不存在")
        self.chunk_size = min(packet.max_write_without_response_size, 244)

    def on_notification(self, _sender, data: bytearray) -> None:
        self.responses.put_nowait(bytes(data))

    async def start(self) -> None:
        await self.client.start_notify(DFU_CONTROL_UUID, self.on_notification)
        await self.client.write_gatt_char(DFU_CONTROL_UUID, bytes([OP_SET_PRN, 0, 0]), response=True)

    async def response(self, opcode: int, timeout: float = 15.0) -> bytes:
        while True:
            data = await asyncio.wait_for(self.responses.get(), timeout=timeout)
            if len(data) < 3 or data[0] != OP_RESPONSE:
                continue
            if data[1] != opcode:
                continue
            if data[2] != 0x01:
                detail = f", 扩展码 0x{data[3]:02x}" if data[2] == 0x0B and len(data) > 3 else ""
                raise RuntimeError(f"DFU 指令 0x{opcode:02x} 失败：{RESULTS.get(data[2], hex(data[2]))}{detail}")
            return data

    async def command(self, opcode: int, payload: bytes = b"") -> bytes:
        await self.client.write_gatt_char(DFU_CONTROL_UUID, bytes([opcode]) + payload, response=True)
        return await self.response(opcode)

    async def select(self, object_type: int) -> tuple[int, int, int]:
        response = await self.command(OP_SELECT, bytes([object_type]))
        if len(response) < 15:
            raise RuntimeError(f"DFU SELECT 响应长度异常：{response.hex()}")
        return struct.unpack_from("<III", response, 3)

    async def send_bytes(self, data: bytes) -> None:
        for offset in range(0, len(data), self.chunk_size):
            await self.client.write_gatt_char(
                DFU_PACKET_UUID,
                data[offset : offset + self.chunk_size],
                response=False,
            )
            await asyncio.sleep(0)

    async def checksum(self) -> tuple[int, int]:
        response = await self.command(OP_CRC)
        if len(response) < 11:
            raise RuntimeError(f"DFU CRC 响应长度异常：{response.hex()}")
        return struct.unpack_from("<II", response, 3)

    async def transfer_init(self, data: bytes) -> None:
        _max_size, offset, crc = await self.select(OBJECT_COMMAND)
        expected_crc = zlib.crc32(data[:offset]) & 0xFFFFFFFF
        if offset > len(data) or crc != expected_crc:
            raise RuntimeError("启动器中残留的 init packet 状态不一致")
        if offset < len(data):
            if offset != 0:
                raise RuntimeError("启动器中存在未完成的 init packet")
            await self.command(OP_CREATE, bytes([OBJECT_COMMAND]) + struct.pack("<I", len(data)))
            await self.send_bytes(data)
            actual_offset, actual_crc = await self.checksum()
            if actual_offset != len(data) or actual_crc != (zlib.crc32(data) & 0xFFFFFFFF):
                raise RuntimeError("init packet CRC 校验失败")
        await self.command(OP_EXECUTE)
        print("升级描述包校验通过")

    async def transfer_firmware(self, firmware: bytes) -> None:
        max_size, offset, crc = await self.select(OBJECT_DATA)
        if offset > len(firmware) or crc != (zlib.crc32(firmware[:offset]) & 0xFFFFFFFF):
            raise RuntimeError("启动器中的固件续传状态不一致")
        if offset % max_size:
            raise RuntimeError(f"启动器中有未完成的数据块（offset={offset}）")

        total = len(firmware)
        while offset < total:
            size = min(max_size, total - offset)
            await self.command(OP_CREATE, bytes([OBJECT_DATA]) + struct.pack("<I", size))
            await self.send_bytes(firmware[offset : offset + size])
            actual_offset, actual_crc = await self.checksum()
            expected_offset = offset + size
            expected_crc = zlib.crc32(firmware[:expected_offset]) & 0xFFFFFFFF
            if actual_offset != expected_offset or actual_crc != expected_crc:
                raise RuntimeError(
                    f"固件 CRC 校验失败：offset={actual_offset}/{expected_offset}, "
                    f"crc=0x{actual_crc:08x}/0x{expected_crc:08x}"
                )
            await self.command(OP_EXECUTE)
            offset = expected_offset
            print(f"已写入 {offset}/{total} 字节（{offset * 100 / total:.1f}%）")


async def install(
    package_path: Path,
    name_prefix: str,
    scan_timeout: float = 30.0,
    known_address: str | None = None,
) -> None:
    package = load_package(package_path)
    print(
        f"升级包：{package_path.name}，固件 {len(package.firmware)} 字节"
        + (f"，版本 {package.version}" if package.version is not None else "")
    )
    device, is_bootloader = await find_device_or_bootloader(name_prefix, scan_timeout, known_address)
    if is_bootloader:
        bootloader = device
        print("设备已经处于 DFU 模式，继续上次升级")
    else:
        print(f"连接 {device.name} ({device.address})")
        await enter_bootloader(device)
        await asyncio.sleep(1.0)
        bootloader = await find_bootloader(exclude_address=device.address)

    disconnected = asyncio.Event()
    async with BleakClient(bootloader, disconnected_callback=lambda _client: disconnected.set()) as client:
        dfu = SecureDFU(client)
        await dfu.start()
        print(f"DFU 数据块大小：{dfu.chunk_size} 字节")
        await dfu.transfer_init(package.init_packet)
        await dfu.transfer_firmware(package.firmware)
        try:
            await asyncio.wait_for(disconnected.wait(), timeout=10.0)
        except TimeoutError:
            pass
    print("固件写入完成，设备正在重启")


async def recover_during_brief_window(
    package_path: Path,
    name_prefix: str,
    recovery_timeout: float,
    known_address: str | None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + recovery_timeout
    attempt = 0
    while loop.time() < deadline:
        attempt += 1
        remaining = deadline - loop.time()
        try:
            await install(
                package_path,
                name_prefix,
                scan_timeout=min(15.0, remaining),
                known_address=known_address,
            )
            return
        except Exception as exc:
            print(f"第 {attempt} 次捕获未完成：{exc}")
            if loop.time() < deadline:
                await asyncio.sleep(0.5)
    raise RuntimeError(f"{recovery_timeout:.0f} 秒监听期内没有完成恢复")


def main() -> None:
    parser = argparse.ArgumentParser(description="通过 Mac/PC 内置蓝牙写入 Nordic nRF5 Secure DFU 固件")
    parser.add_argument("package", type=Path, help="nrfutil 生成的 application DFU ZIP")
    parser.add_argument("--name-prefix", default="NRF_EPD", help="设备蓝牙名称前缀")
    parser.add_argument("--known-address", help="macOS CoreBluetooth 缓存的应用设备标识")
    parser.add_argument(
        "--recovery-timeout",
        type=float,
        default=0,
        help="持续监听短暂广播并反复尝试恢复的秒数；0 表示只尝试一次",
    )
    args = parser.parse_args()
    package = args.package.expanduser().resolve()
    if args.recovery_timeout > 0:
        asyncio.run(
            recover_during_brief_window(
                package,
                args.name_prefix,
                args.recovery_timeout,
                args.known_address,
            )
        )
    else:
        asyncio.run(install(package, args.name_prefix, known_address=args.known_address))


if __name__ == "__main__":
    main()
