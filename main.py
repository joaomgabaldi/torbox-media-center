from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from functions.appFunctions import bootUp, getMountMethod, getAllUserDownloadsFresh, getMountRefreshTime
from functions.databaseFunctions import closeAllDatabases
import logging
from sys import platform
import subprocess
import time

class WindowsWSLNotifyHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.last_notify_time = 0
        self.cooldown_seconds = 60

    def send_notification(self, title, message, severity):
        safe_msg = message.replace("'", "").replace('"', '')
        
        if severity == "Info":
            sys_icon = "Information"
            tt_icon = "Info"
        elif severity == "Error":
            sys_icon = "Error"
            tt_icon = "Error"
        else:
            sys_icon = "Warning"
            tt_icon = "Warning"

        ps_script = f"""
        [reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null;
        [reflection.assembly]::loadwithpartialname('System.Drawing') | Out-Null;
        $notify = New-Object system.windows.forms.notifyicon;
        $notify.icon = [System.Drawing.SystemIcons]::{sys_icon};
        $notify.visible = $True;
        $notify.showballoontip(10, '{title}', '{safe_msg}', [system.windows.forms.tooltipicon]::{tt_icon});
        Start-Sleep -s 7;
        $notify.Dispose();
        """
        try:
            subprocess.Popen(["powershell.exe", "-Command", ps_script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def emit(self, record):
        msg = record.getMessage()
        
        if "TORBOX_STARTUP_SUCCESS" in msg:
            self.send_notification("TorBox Media Center", "Serviço iniciado e rodando em segundo plano.", "Info")
            return

        if record.levelno >= logging.ERROR:
            current_time = time.time()
            if current_time - self.last_notify_time >= self.cooldown_seconds:
                self.last_notify_time = current_time
                self.send_notification("TorBox Alerta (Erro)", msg, "Error")
        elif record.levelno >= logging.WARNING and "429" in msg:
            current_time = time.time()
            if current_time - self.last_notify_time >= self.cooldown_seconds:
                self.last_notify_time = current_time
                self.send_notification("TorBox Alerta (Rate Limit)", msg, "Warning")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s,%(msecs)03d %(name)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logging.getLogger("httpx").setLevel(logging.WARNING)

if platform == "linux" or platform == "linux2":
    wsl_handler = WindowsWSLNotifyHandler()
    logging.getLogger().addHandler(wsl_handler)

if __name__ == "__main__":
    bootUp()
    mount_method = getMountMethod()

    if mount_method == "strm":
        scheduler = BlockingScheduler()
    elif mount_method == "fuse":
        if platform == "win32":
            logging.error("The FUSE mount method is not supported on Windows. Please use the STRM mount method or run this application on a Linux system.")
            exit(1)
        scheduler = BackgroundScheduler()
    else:
        logging.error("Invalid mount method specified.")
        exit(1)

    user_downloads = getAllUserDownloadsFresh()

    scheduler.add_job(
        getAllUserDownloadsFresh,
        "interval",
        hours=getMountRefreshTime(),
        id="get_all_user_downloads_fresh",
    )

    try:
        logging.info("Starting scheduler and mounting...")
        if mount_method == "strm":
            from functions.stremFilesystemFunctions import runStrm
            runStrm()
            scheduler.add_job(
                runStrm,
                "interval",
                minutes=5,
                id="run_strm",
            )
            logging.info("TORBOX_STARTUP_SUCCESS")
            scheduler.start()
        elif mount_method == "fuse":
            from functions.fuseFilesystemFunctions import runFuse
            scheduler.start()
            logging.info("TORBOX_STARTUP_SUCCESS")
            runFuse()
    except (KeyboardInterrupt, SystemExit):
        if mount_method == "fuse":
            from functions.fuseFilesystemFunctions import unmountFuse
            unmountFuse()
        elif mount_method == "strm":
            from functions.stremFilesystemFunctions import unmountStrm
            unmountStrm()
        closeAllDatabases()
        exit(0)