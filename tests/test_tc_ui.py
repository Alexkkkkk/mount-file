# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Interface test for the Total Commander window (offscreen, no display).

Builds a synthetic FAT32 image with files and directories, opens it in the
window, and checks the things the interface promises: five columns with the
Total Commander headers, directory rows reading '<Папка>', the '-a--c' /
'----c' attribute column, the free-space readout, the per-panel status line,
marking, sorting, F7 inside the image and the hex viewer data path.

It also renders reference screenshots of both themes so the layout can be
looked at without a Windows box:

    QT_QPA_PLATFORM=offscreen python3 tests/test_tc_ui.py
    -> /home/user/ui_shots/mountfile-dark.png, mountfile-light.png
"""

import os
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MOUNTFILE_UI_SHOTS", "/home/user/ui_shots")

SHOT_DIR = os.environ.get("MOUNTFILE_UI_SHOTS", "/home/user/ui_shots")

BPS, SPC, RESERVED, NFATS = 512, 1, 32, 1
TOTAL_SECTORS = 70000
PART_LBA = 2048
PART_BASE = PART_LBA * BPS


def _spf():
    spf = 600
    while True:
        clusters = TOTAL_SECTORS - (RESERVED + NFATS * spf)
        if (clusters + 2) * 4 <= spf * BPS and clusters >= 65525:
            return spf
        spf += 1


SPF = _spf()


def make_fat32_image(path):
    """Raw image with an MBR and one FAT32 partition at LBA 2048."""
    with open(path, "wb") as f:
        f.truncate(PART_BASE + TOTAL_SECTORS * BPS)
        mbr = bytearray(BPS)
        mbr[446 + 4] = 0x0B
        struct.pack_into("<I", mbr, 446 + 8, PART_LBA)
        struct.pack_into("<I", mbr, 446 + 12, TOTAL_SECTORS)
        mbr[510:512] = b"\x55\xaa"
        f.seek(0)
        f.write(mbr)
        bpb = bytearray(BPS)
        bpb[0:3] = b"\xeb\x58\x90"
        bpb[3:11] = b"MSWIN4.1"
        struct.pack_into("<H", bpb, 11, BPS)
        bpb[13] = SPC
        struct.pack_into("<H", bpb, 14, RESERVED)
        bpb[16] = NFATS
        struct.pack_into("<I", bpb, 32, TOTAL_SECTORS)
        struct.pack_into("<I", bpb, 36, SPF)
        struct.pack_into("<I", bpb, 44, 2)
        bpb[64] = 0x80
        bpb[510:512] = b"\x55\xaa"
        f.seek(PART_BASE)
        f.write(bpb)
        fat = bytearray(SPF * BPS)
        struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
        struct.pack_into("<I", fat, 4, 0xFFFFFFFF)
        struct.pack_into("<I", fat, 8, 0x0FFFFFFF)
        f.seek(PART_BASE + RESERVED * BPS)
        f.write(fat)


def fill_image(path):
    """Write the demo tree with the FAT32 engine (also tests the writer)."""
    from mountfile.fat32 import Fat32

    with open(path, "r+b") as fh:
        fs = Fat32(fh, offset=PART_BASE)
        fs.mkdir("/", "system")
        fs.mkdir("/", "apps")
        fs.write_file("/", "boot.bin", b"\x00\xff" * 32768)
        fs.write_file("/", "uImage", os.urandom(1_200_000))
        fs.write_file("/", "rootfs.jffs2", os.urandom(1_800_000))
        fs.write_file("/", "config.ini", b"[net]\nip=192.168.1.10\n")
        fs.write_file("/", "README.TXT", b"Mount File demo image\n")
        fs.write_file("/system", "kernel.bin", os.urandom(900_000))
        fs.write_file("/system", "init", b"#!/bin/sh\nmount -a\n")
        fs.write_file("/apps", "webui.tar.gz", os.urandom(240_000))
        return fs


def make_local_dir(root):
    """A left-hand 'local' panel needs something to show as well."""
    names = (
        ("firmware_2024-06-11.dump", 4_100_000),
        ("nand_dump_good.bin", 2_048_000),
        ("notes.txt", 1200),
        ("checksums.sha256", 340),
        ("logs", None),
    )
    stamps = (1718067600, 1718068000, 1717981200, 1717981300, 1718154000)
    for (name, size), stamp in zip(names, stamps):
        target = os.path.join(root, name)
        if size is None:
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "mount.log"), "w", encoding="utf-8") as f:
                f.write("mounted volume 0\n")
        else:
            with open(target, "wb") as f:
                f.write(b"\x00" * min(size, 4096))
            os.utime(target, (stamp, stamp))


def _cells(panel):
    """All rows of a panel as plain text, cell by cell."""
    table = panel.table
    out = []
    for r in range(table.rowCount()):
        row = []
        for c in range(table.columnCount()):
            item = table.item(r, c)
            row.append(item.text() if item is not None else "")
        out.append(row)
    return out


def main():
    # Only for this process: the environment switches are set here and not at
    # import time, otherwise pytest would collect this module, flip
    # MOUNTFILE_NO_UPDATE on and break tests/test_updater.py.
    os.environ["MOUNTFILE_NO_UPDATE"] = "1"

    from mountfile import tc_style
    from mountfile.qt_compat import QApplication
    from mountfile.tc_window import COLUMNS, DIR_SIZE_TEXT, MainWindow

    tc_style.load_settings()
    tc_style.SETTINGS["theme"] = "dark"
    tc_style.SETTINGS["show_log"] = False
    tc_style.SETTINGS_PATH = os.path.join(tempfile.gettempdir(), "mountfile-ui.json")

    app = QApplication(sys.argv)
    win = MainWindow()

    with tempfile.TemporaryDirectory() as tmp:
        image = os.path.join(tmp, "nand_dump.img")
        make_fat32_image(image)
        fill_image(image)
        make_local_dir(tmp)

        assert win.load_image(image), "образ не открылся"
        for _ in range(5):
            app.processEvents()

        left, right = win.panelL, win.panelR
        print("томов найдено:", [v.desc for v in win.volumes])
        assert len(win.volumes) == 1, win.volumes
        assert [v.desc for v in win.volumes] == [
            v.desc for v in win.volumes if "FAT32" in v.desc
        ], win.volumes
        assert left.mode == "img", left.mode
        assert left.volume_offset == PART_BASE, left.volume_offset
        right.cd(tmp)
        app.processEvents()

        # ---- columns ---------------------------------------------------
        headers = [
            left.table.horizontalHeaderItem(c).text()
            for c in range(left.table.columnCount())
        ]
        assert headers == list(COLUMNS), headers
        print("колонки:", " | ".join(headers))

        # ---- image panel listing ---------------------------------------
        rows = {r[0]: r for r in _cells(left)}
        print("левая панель:", list(rows)[:9])
        assert "boot.bin" in rows, "файлы образа не видны"
        assert rows["system"][2] == DIR_SIZE_TEXT, rows["system"]
        assert rows["system"][1] == "", rows["system"]
        assert rows["boot.bin"][1] == "BIN", rows["boot.bin"]
        assert rows["boot.bin"][2] == "65 536", rows["boot.bin"]
        assert rows["boot.bin"][4] == "-a--c", rows["boot.bin"]
        assert rows["system"][4] == "----c", rows["system"]
        boot_row = left._row_of("boot.bin")
        assert left.table.item(boot_row, 0).toolTip() == "boot.bin"
        # at the root of a volume there is no parent row - '..' appears only
        # inside a subdirectory (same as Total Commander)
        assert ".." not in rows, list(rows)

        # ---- directories are navigable --------------------------------
        left.cd("/system")
        names = [r[0] for r in _cells(left)]
        assert "kernel.bin" in names, names
        assert names[0] == "..", names
        up_row = _cells(left)[0]
        assert up_row[2] == DIR_SIZE_TEXT and up_row[4] == "----c", up_row
        left.go_up()
        assert left.path == "/", left.path

        # ---- local panel ----------------------------------------------
        lrows = {r[0]: r for r in _cells(right)}
        assert "firmware_2024-06-11.dump" in lrows, list(lrows)
        firmware_row = right._row_of("firmware_2024-06-11.dump")
        assert (
            right.table.item(firmware_row, 0).toolTip()
            == "firmware_2024-06-11.dump"
        )
        assert lrows["logs"][2] == DIR_SIZE_TEXT, lrows["logs"]
        assert lrows["notes.txt"][1] == "TXT", lrows["notes.txt"]
        assert lrows["notes.txt"][2] == "1 200", lrows["notes.txt"]
        assert lrows["notes.txt"][3].count(".") == 2, lrows["notes.txt"]

        # ---- free space + status line ---------------------------------
        space = left.lbl_space.text()
        status = left.lbl_status.text()
        print("свободно:", space)
        print("статус  :", status)
        left.unmark_all()
        assert "файлов: 0 из 8" in status, status
        assert "папок: 0 из 2" in status, status
        assert right.lbl_status.text().startswith("0 Кб из"), right.lbl_status.text()

        # ---- marking changes the status line --------------------------
        left.toggle_mark(3)  # $MBR
        left.toggle_mark(4)  # $OrphanFiles
        assert len(left.marked) == 2, left.marked
        marked_status = left.lbl_status.text()
        print("после отметки:", marked_status)
        assert "файлов: 2 из 8" in marked_status, marked_status
        assert "Кб из" in marked_status
        left.unmark_all()

        # ---- sorting --------------------------------------------------
        def sizes_now():
            return [
                int(r[2].replace(" ", ""))
                for r in _cells(left)
                if r[2] not in (DIR_SIZE_TEXT, "") and r[0] != ".."
            ]

        left.on_header_clicked(2)
        ascending = sizes_now()
        assert ascending == sorted(ascending), ascending
        left.on_header_clicked(2)
        descending = sizes_now()
        assert descending == sorted(descending, reverse=True), descending
        left.on_header_clicked(0)

        # ---- F7 inside the image (FAT32 writer) -----------------------
        left.writer.mkdir("/", "TESTDIR")
        # re-mount the volume so the listing is read back from the image
        assert win.mount_volume(left, PART_BASE), "том не перемонтировался"
        assert "TESTDIR" in [r[0] for r in _cells(left)], "mkdir не сработал"
        print("после F7:", [r[0] for r in _cells(left)])

        # ---- F3 data path ---------------------------------------------
        row = [r[0] for r in _cells(left)].index("README.TXT")
        left.table.setCurrentCell(row, 0)
        entry = left.selected_entry()
        assert entry and entry["name"] == "README.TXT", entry
        data = left.provider.read_file_range("/README.TXT", 0, 64)
        assert data.startswith(b"Mount File demo image"), data

        # ---- local panel status + disk usage -------------------------
        assert right._space_text().endswith("свободно"), right._space_text()

        # ---- themes ---------------------------------------------------
        os.makedirs(SHOT_DIR, exist_ok=True)
        win.resize(1180, 720)
        win.show()
        left.marked = {"boot.bin", "config.ini"}
        left._fill()
        left.table.setCurrentCell(6, -1)
        for theme in ("dark", "light"):
            win._apply_theme(theme)
            right.table.setCurrentCell(1, -1)
            win.set_active("L")
            app.processEvents()
            time.sleep(0.15)
            app.processEvents()
            shot = os.path.join(SHOT_DIR, "mountfile-%s.png" % theme)
            assert win.grab().save(shot), "скриншот не сохранился"
            print("скриншот:", shot, os.path.getsize(shot), "байт")
        win._apply_theme("dark")

        # ---- the settings dialog and its switches ---------------------
        from mountfile.tc_window import SettingsDialog

        dlg = SettingsDialog(win)
        assert dlg.spn_font.minimum() == 7 and dlg.spn_font.maximum() == 16
        assert dlg.cmb_theme.count() == 2, dlg.cmb_theme.count()
        dlg.deleteLater()

        win._close_writer()
        win.close()

    print("UI OK: интерфейс Total Commander собран и проверен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
