# -*- coding: utf-8 -*-
"""
pc_time_tracker.pyw  —  Трекер времени, проведённого за ПК (Windows).

Архитектура:
  1. Активность определяется через Windows API GetLastInputInfo (user32.dll)
     с помощью встроенного модуля ctypes. Никаких pynput/pyautogui — только
     легковесный системный вызов, который не триггерит антивирусы.
  2. Фоновый режим: консоль скрывается через FreeConsole + ShowWindow.
     Рекомендуемые способы запуска: pythonw.exe или .py/.pyw-файл,
     скомпилированный PyInstaller'ом в режиме --noconsole.
  3. Автозагрузка: запись в реестр HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
     (флаг --install-autostart). В реестре путь ведёт к pythonw.exe (или .exe),
     чтобы при старте Windows не мелькало чёрное окно консоли.
  4. Данные хранятся в SQLite (стандартная библиотека sqlite3):
     %APPDATA%\\PCTimeTracker\\stats.db
     Структура: дата | общее время за ПК (сек) | активное время без учёта AFK (сек).
     Рядом пишется человекочитаемый лог logs/tracker.log.
  5. Системный трей: pystray + Pillow. Иконка генерируется программно (круг с
     буквой «Ч»). Правый клик -> меню: «Статистика за сегодня»,
     «Открыть папку с логами», «Выход».

Зависимости:  pip install pystray Pillow
Сборка EXE:  pyinstaller --noconsole --onefile pc_time_tracker.pyw
"""

import argparse
import ctypes
import datetime as dt
import os
import sqlite3
import subprocess
import sys
import time

# ============================ КОНСТАНТЫ =====================================

APP_NAME = "PCTimeTracker"          # имя папки в %APPDATA%
IDLE_THRESHOLD_SEC = 300            # 5 минут простоя => пользователь «отошёл»
POLL_INTERVAL_SEC = 60              # проверка раз в минуту (щадящий режим CPU)
MIN_TICK_SEC = 1                    # защита от аномально малых интервалов
MAX_TICK_SEC = 3600                 # защита от «прыжков» часов (макс. час за тик)

# ============================================================================
#  БЛОК 1. СКРЫТИЕ КОНСОЛИ (фоновый режим)
# ============================================================================


def hide_console():
    """Прячет консольное окно, если скрипт запущен через обычный python.exe.

    При запуске через pythonw.exe или PyInstaller --noconsole окна просто нет,
    и функция молча ничего не делает (все ошибки перехватываются).
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()          # handle консоли (0, если её нет)
        if hwnd:
            SW_HIDE = 0
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, SW_HIDE)        # скрыть окно
            kernel32.FreeConsole()                  # полностью отвязать процесс от консоли
    except Exception:
        # Любые сбои не должны ронять трекер — это best-effort операция.
        pass


# ============================================================================
#  БЛОК 2. ИЗМЕРЕНИЕ ПРОСТОЯ ЧЕРЕЗ WINDOWS API (ctypes)
# ============================================================================


class LASTINPUTINFO(ctypes.Structure):
    """Структура WinAPI tagLASTINPUTINFO (winuser.h)."""
    _fields_ = [
        ("cbSize", ctypes.c_uint),      # размер структуры в байтах
        ("dwTime", ctypes.c_uint),      # tickCount момента последнего ввода
    ]


def get_idle_seconds():
    """Возвращает количество секунд с момента последнего движения мыши/нажатия клавиши.

    Используется ТОЛЬКО GetLastInputInfo из user32.dll — один лёгкий вызов,
    без хуков, поэтому антивирусы не считают это шпионским ПО.
    """
    if sys.platform != "win32":
        # Вне Windows API недоступен: считаем, что пользователь активен,
        # чтобы скрипт можно было тестировать на других ОС.
        return 0.0
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0  # не удалось получить данные — консервативно считаем «активен»
        # GetTickCount() и dwTime оба отсчитываются от старта системы, мс.
        millis = kernel32.GetTickCount() - info.dwTime
        return max(0.0, millis / 1000.0)
    except Exception:
        return 0.0


# ============================================================================
#  БЛОК 3. ХРАНИЛИЩЕ ДАННЫХ (SQLite в %APPDATA%)
# ============================================================================


def get_app_data_dir():
    """Папка данных приложения: %APPDATA%\\PCTimeTracker (с fallback)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


class Storage:
    """Обёртка над SQLite: одна строка на день (PK по дате)."""

    def __init__(self, db_path=None):
        self.dir = get_app_data_dir()
        self.db_path = db_path or os.path.join(self.dir, "stats.db")
        self.log_path = os.path.join(self.dir, "logs", "tracker.log")
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with self._conn() as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS daily_stats (
                       date TEXT PRIMARY KEY,               -- 'ГГГГ-ММ-ДД'
                       total_online REAL NOT NULL DEFAULT 0, -- всё время, пока ПК был включён (сек)
                       active_time  REAL NOT NULL DEFAULT 0  -- время без учёта AFK-пауз (сек)
                   )"""
            )

    def _conn(self):
        # timeout=15 — подождать чужих блокировок, а не падать сразу
        return sqlite3.connect(self.db_path, timeout=15)

    def add_tick(self, day, total_sec, active_sec):
        """Инкрементально добавляет секунды к записи за указанный день."""
        with self._conn() as con:
            con.execute(
                """INSERT INTO daily_stats (date, total_online, active_time)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                       total_online = total_online + excluded.total_online,
                       active_time  = active_time  + excluded.active_time""",
                (day, total_sec, active_sec),
            )

    def get_day(self, day):
        """Возвращает (total_online, active_time) в секундах для даты."""
        with self._conn() as con:
            row = con.execute(
                "SELECT total_online, active_time FROM daily_stats WHERE date = ?",
                (day,),
            ).fetchone()
        return row if row else (0.0, 0.0)

    def log(self, message):
        """Пишет строку в лог-файл (для отладки и истории)."""
        try:
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {message}\n")
        except Exception:
            pass  # логирование не должно ломать основную работу


def format_hms(seconds):
    """Красивое представление длительности: '3 ч 27 мин 05 сек'."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h} ч {m:02d} мин {s:02d} сек"


# ============================================================================
#  БЛОК 4. АВТОЗАГРУЗКА (реестр HKCU ...\CurrentVersion\Run)
# ============================================================================

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _is_frozen():
    """True, если запущен собранный PyInstaller исполняемый файл."""
    return getattr(sys, "frozen", False)


def build_autostart_command():
    """Формирует команду автозапуска, ВСЕГДА без видимой консоли.

    - Собранный .exe: запускаем сам exe напрямую (он и так --noconsole).
    - Обычный скрипт: подменяем python.exe -> pythonw.exe (GUI-интерпретатор
      без консоли) и передаём путь к этому скрипту.
    """
    if _is_frozen():
        exe = os.path.abspath(sys.executable)
        return f'"{exe}"'
    script = os.path.abspath(__file__)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        raise FileNotFoundError(
            f"Не найден pythonw.exe рядом с {sys.executable}. "
            "Установите полное Python-distribution или соберите .exe через PyInstaller."
        )
    return f'"{pythonw}" "{script}"'


def install_autostart():
    """Однократная установка в автозагрузку (HKCU — права администратора НЕ нужны)."""
    try:
        import winreg  # только Windows
    except ImportError:
        print("[!] Установка автозагрузки доступна только на Windows.")
        return False
    try:
        cmd = build_autostart_command()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        with key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        print(f"[OK] Добавлено в автозагрузку:\n     {cmd}")
        return True
    except Exception as e:
        print(f"[!] Не удалось добавить в автозагрузку: {e}")
        return False


def remove_autostart():
    """Удаляет нашу запись из автозагрузки (флаг --uninstall-autostart)."""
    try:
        import winreg
    except ImportError:
        print("[!] Доступно только на Windows.")
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        with key:
            winreg.DeleteValue(key, APP_NAME)
        print("[OK] Запись удалена из автозагрузки.")
        return True
    except FileNotFoundError:
        print("[i] Записи в автозагрузке не найдено — удалять нечего.")
        return False
    except Exception as e:
        print(f"[!] Не удалось удалить запись: {e}")
        return False


def show_status():
    """Вывод статистики за сегодня в консоль (флаг --status)."""
    storage = Storage()
    today = dt.date.today().isoformat()
    total, active = storage.get_day(today)
    print(f"Статистика за {today}:")
    print(f"  Общее время за ПК : {format_hms(total)}")
    print(f"  Активное время    : {format_hms(active)}")


# ============================================================================
#  БЛОК 5. ТРЕЙ-ИНТЕРФЕЙС (pystray + Pillow)
# ============================================================================


def create_icon_image(color=(30, 144, 255)):
    """Программно рисует иконку: залитый круг с белой буквой «Ч» (часы).

    Генерация через PIL избавляет пользователя от скачивания внешних картинок.
    """
    from PIL import Image, ImageDraw  # импорт здесь, чтобы CLI-флаги работали без Pillow

    size = (64, 64)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 4
    draw.ellipse([pad, pad, size[0] - pad, size[1] - pad], fill=color + (255,))
    # Тонкая белая обводка для читаемости на тёмном фоне трея
    draw.ellipse([pad, pad, size[0] - pad, size[1] - pad], outline=(255, 255, 255, 255), width=2)
    # Буква рисуется дефолтным шрифтом; при желании замените на ImageFont.truetype(...)
    try:
        text = "Ч"
        tw = draw.textbbox((0, 0), text)
        w, h = tw[2] - tw[0], tw[3] - tw[1]
        draw.text(((size[0] - w) / 2 - tw[0], (size[1] - h) / 2 - tw[1]),
                  text, fill=(255, 255, 255, 255))
    except Exception:
        pass  # если шрифт не вывелся — круглая иконка тоже допустима
    return img


def popup(title, text):
    """Всплывающее сообщение: сначала нативный MessageBoxW, затем fallback."""
    if sys.platform == "win32":
        try:
            MB_OK = 0x0
            MB_ICONINFORMATION = 0x40
            MB_SETFOREGROUND = 0x10000  # окно поверх остальных, его точно видно
            ctypes.windll.user32.MessageBoxW(0, text, title,
                                             MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND)
            return
        except Exception:
            pass
    # Fallback для разработки вне Windows: tkinter или печать в консоль
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, text)
        root.destroy()
    except Exception:
        print(f"{title}: {text}")


def open_logs_folder():
    """Открывает папку с логом/БД в проводнике Windows."""
    folder = os.path.dirname(Storage().log_path)
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # штатный «Проводник -> открыть папку»
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception as e:
        popup(APP_NAME, f"Не удалось открыть папку:\n{folder}\n\n{e}")


def today_report(storage):
    """Текст отчёта за сегодня для всплывающего окна."""
    today = dt.date.today().isoformat()
    total, active = storage.get_day(today)
    idle_ratio = (active / total * 100.0) if total > 0 else 0.0
    return (
        f"Дата: {today}\n\n"
        f"Общее время за ПК : {format_hms(total)}\n"
        f"Активное время    : {format_hms(active)}\n"
        f"AFK (отошёл)      : {format_hms(max(0.0, total - active))}\n"
        f"Доля активности   : {idle_ratio:.1f}%"
    )


def run_with_tray(storage):
    """Основной запуск: фоновый поток-сборщик + иконка в трее (блокирующий mainloop)."""
    import threading

    import pystray
    from pystray import MenuItem as Item

    def stats_item(_icon, _item):
        popup("Статистика за сегодня", today_report(storage))

    def open_logs_item(_icon, _item):
        open_logs_folder()

    def quit_item(icon, _item):
        storage.log("Пользователь вышел через меню трея.")
        icon.stop()  # останавливает notify_loop и mainloop

    def make_menu(icon):
        today = dt.date.today().isoformat()
        total, active = storage.get_day(today)
        title = f"За сегодня: {format_hms(total)} (активно {format_hms(active)})"
        return pystray.Menu(
            Item(title, None, enabled=False),   # информационная строка
            pystray.Menu.SEPARATOR,
            Item("Статистика за сегодня", stats_item, default=True),  # двойной клик = она же
            Item("Открыть папку с логами", open_logs_item),
            Item("Выход", quit_item),
        )

    icon = pystray.Icon(APP_NAME, create_icon_image(), "Трекер времени за ПК", menu=make_menu)

    worker = threading.Thread(target=tracking_loop, args=(storage, icon),
                              daemon=True, name="tracker-loop")
    worker.start()

    try:
        icon.run()  # блокирующий цикл сообщений трея
    except KeyboardInterrupt:
        icon.stop()
    finally:
        storage.log("Трекер остановлен.")


# ============================================================================
#  БЛОК 6. ФОНОВЫЙ ЦИКЛ СБОРА СТАТИСТИКИ
# ============================================================================


def tracking_loop(storage, icon=None):
    """Цикл: раз в POLL_INTERVAL_SEC замеряем простой и копим секунды в SQLite.

    Логика порога AFK:
      idle < 300 c  -> пользователь ЗА ПК: засчитываем и total, и active;
      idle >= 300 c -> пользователь ОТОШЁЛ: засчитываем только total (он ведь
                      не ушёл с работы, а просто отходил — машина работала).
    """
    last_beat = time.monotonic()
    was_active = None  # для записи только переходов состояния (не спамим в лог)

    while True:
        try:
            now = time.monotonic()
            elapsed = now - last_beat
            last_beat = now
            #Sanity check: корректные границы одного тика (защита от сна/прыжков часов)
            elapsed = min(max(elapsed, MIN_TICK_SEC), MAX_TICK_SEC)

            idle = get_idle_seconds()
            is_active = idle < IDLE_THRESHOLD_SEC
            day = dt.date.today().isoformat()

            storage.add_tick(day, elapsed, elapsed if is_active else 0.0)

            if is_active != was_active:  # произошло переключение активность/AFK
                state = "АКТИВЕН" if is_active else "AFK (отошёл)"
                storage.log(f"Состояние: {state} (idle={idle:.0f}s)")
                was_active = is_active

            if icon is not None:
                try:
                    total, active = storage.get_day(day)
                    icon.title = (f"Трекер времени за ПК\n"
                                  f"Сегодня: {format_hms(total)}\n"
                                  f"Активно: {format_hms(active)}\n"
                                  f"Сейчас: {'работаю' if is_active else 'AFK'}")
                    icon.update_menu()  # обновить подпись со статистикой в меню
                except Exception:
                    pass  # обновление тултипа не критично
        except KeyboardInterrupt:
            break
        except Exception as e:
            # Любая ошибка логируется, но убивать трекер мы не имеем права.
            try:
                storage.log(f"ОШИБКА в цикле: {e!r}")
            except Exception:
                pass

        time.sleep(POLL_INTERVAL_SEC)  # спим минуту — нагрузка на CPU ~0%


# ============================================================================
#  БЛОК 7. ТОЧКА ВХОДА
# ============================================================================


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="pc_time_tracker",
        description="Трекер времени, проведённого за ПК (трей + SQLite + автозагрузка).",
    )
    parser.add_argument("--install-autostart", action="store_true",
                        help="Добавить трекер в автозагрузку Windows (HKCU\\...\\Run)")
    parser.add_argument("--uninstall-autostart", action="store_true",
                        help="Удалить трекер из автозагрузки Windows")
    parser.add_argument("--status", action="store_true",
                        help="Показать статистику за сегодня в консоли")
    parser.add_argument("--show-console", action="store_true",
                        help="Не прятать консоль (режим отладки)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # --- Одноразовые CLI-действия (работают даже без Pillow/pystray) ---
    if args.install_autostart:
        sys.exit(0 if install_autostart() else 1)
    if args.uninstall_autostart:
        sys.exit(0 if remove_autostart() else 1)
    if args.status:
        show_status()
        sys.exit(0)

    # --- Обычный запуск трекера ---
    if not args.show_console:
        hide_console()  # прячем чёрное окно, если запустили через python.exe

    storage = Storage()
    storage.log("=== Запуск трекера ===")

    try:
        run_with_tray(storage)  # pystray.mainloop блокирует поток до «Выход»
    except ImportError as e:
        # Трейд-оф безопасности: если pystray/Pillow не установлены,
        # НЕ молчим — показываем ошибку и продолжаем считать в CLI-цикле.
        msg = (f"Не установлена библиотека для трея ({e.name or e}).\n"
               f"Установите:  pip install pystray Pillow\n"
               f"Трекер продолжает работать в фоновом режиме без иконки.")
        storage.log(msg.replace("\n", " | "))
        popup(APP_NAME, msg)
        tracking_loop(storage)  # деградация: сбор статистики без GUI
    except Exception as e:
        storage.log(f"Критическая ошибка: {e!r}")
        raise


if __name__ == "__main__":
    main()
