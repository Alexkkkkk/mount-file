
## Opt-in libguestfs для NTFS/ext и изолированное монтирование

Для NTFS и ext2/3/4 можно явно включить backend `libguestfs`. Образ не
монтируется в host OS: `mount()`/`mount_options()` работают внутри appliance
libguestfs, а `unmount()`, `umount_all()` и `shutdown()` освобождают ресурсы и
сбрасывают кэши перед `close()`.

```python
from mountfile.universal_fs_rw import UniversalFSRW

with UniversalFSRW(
    "disk.img",
    allow_libguestfs=True,
    auto_backup=True,
) as fs:
    fs.mount()                         # уже смонтировано при открытии backend
    fs.write_file("/new_file.txt", b"hello\n")
    fs.unmount()                       # размонтирование внутри appliance
    fs.shutdown()                      # sync + shutdown appliance
```

Для CLI используется явный opt-in:

```bash
mountfile put disk.img ./local.txt --dest / --yes --allow-libguestfs
```

На Debian/Ubuntu нужны системные пакеты `python3-libguestfs`,
`libguestfs-tools` и, для NTFS, `ntfs-3g`. Если стандартное монтирование NTFS
не проходит, backend автоматически пробует опцию `ntfs-3g`. Без
`allow_libguestfs=True` / `--allow-libguestfs` внешний writer не выбирается.
