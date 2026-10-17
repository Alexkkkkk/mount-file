# MOUNTFILE_FIX — OLMA rules
"""База правил для статического анализа."""

from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    INFO = 1
    WARN = 2
    ERROR = 3
    CRITICAL = 4

    def label(self) -> str:
        return {1: "INFO", 2: "WARN", 3: "ERROR", 4: "CRITICAL"}[int(self)]


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: Severity
    pattern: str       # "open_no_with", "read_no_size", ...
    fixable: bool
    hint: str


RULES: dict[str, Rule] = {
    "OL001": Rule("OL001", "open() без with", Severity.WARN, "open_no_with", True,
                  "Оберните open() в with для гарантированного закрытия"),
    "OL002": Rule("OL002", "read() без размера", Severity.WARN, "read_no_size", True,
                  "Ограничьте size, чтобы не читать весь файл в память"),
    "OL003": Rule("OL003", "seek() без проверки", Severity.INFO, "seek_unchecked", True,
                  "Проверьте границы перед seek()"),
    "OL004": Rule("OL004", "f.read() в цикле без break", Severity.WARN, "read_no_break", False,
                  "Добавьте условие выхода, чтобы избежать бесконечного цикла"),
    "OL005": Rule("OL005", "запись в файл без flush/close", Severity.WARN, "write_no_close", True,
                  "Используйте with или явный flush()"),
    "OL006": Rule("OL006", "захардкоженный путь", Severity.INFO, "hardcoded_path", False,
                  "Вынесите путь в константу или аргумент"),
    "OL007": Rule("OL007", "IO без обработки исключений", Severity.WARN, "io_no_try", True,
                  "Оберните в try/except OSError"),
    "OL008": Rule("OL008", "двойной close", Severity.INFO, "double_close", False,
                  "Проверьте, не закрывается ли handle дважды"),
    "OL009": Rule("OL009", "чтение всего файла в память", Severity.WARN, "read_all", True,
                  "Используйте потоковое чтение chunk'ами"),
    "OL010": Rule("OL010", "проверка границ без инварианта", Severity.ERROR, "manual_bounds", True,
                  "Используйте OlmaBuffer/OlmaView для проверки границ"),
    "OL011": Rule("OL011", "FAT-цепочка без бюджета", Severity.CRITICAL, "chain_no_budget", True,
                  "Используйте OlmaChain с бюджетом итераций"),
    "OL012": Rule("OL012", "XOR без проверки таблицы", Severity.ERROR, "xor_no_check", True,
                  "Проверьте, что таблица не пуста, через OlmaXorDecoder"),
    "OL013": Rule("OL013", "запись неатомарна", Severity.WARN, "write_nonatomic", True,
                  "Используйте OlmaTransaction для atomic write"),
    "OL014": Rule("OL014", "int.from_bytes без проверки длины", Severity.WARN, "from_bytes_unsafe", False,
                  "Проверьте длину среза перед int.from_bytes"),
    "OL015": Rule("OL015", "os.replace без fsync", Severity.INFO, "replace_no_fsync", False,
                  "Добавьте fsync перед replace"),
    "OL016": Rule("OL016", "молчаливый except", Severity.WARN, "silent_except", False,
                  "Логируйте или перебрасывайте исключение"),
    "OL017": Rule("OL017", "print вместо logging", Severity.INFO, "print_instead_logging", False,
                  "Используйте logging для диагностики"),
    "OL018": Rule("OL018", "магическое число в смещении", Severity.INFO, "magic_offset", False,
                  "Вынесите offset в именованную константу"),
    "OL019": Rule("OL019", "close без try/finally", Severity.INFO, "close_no_finally", True,
                  "Оберните close в try/finally или используйте with"),
    "OL020": Rule("OL020", "open в режиме w без backup", Severity.CRITICAL, "open_w_no_backup", True,
                  "Используйте OlmaTransaction, чтобы не потерять данные"),
}
