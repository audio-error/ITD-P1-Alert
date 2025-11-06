import sys
import os
import json
import queue
import datetime
import re
import threading
from time import sleep

from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QWidget
from PyQt5.QtGui import QFont
from PyQt5.QtCore import Qt, QTimer
from waitress import serve
from flask import Flask, request
import pygame

#load configuaration file
#Curtesy of ChatGPT
import yaml
from pathlib import Path

DEFAULTS = {
    "flask_port": 5002,
    "alert_message": "⚠ Priority 1 Ticket! ⚠",
    "resolved_message": "P1 Ticket Resolved ✓",
    "alert_sound": "P1.wav",
    "resolved_sound": "bomb_defused.wav",
}

BASE = Path(__file__).resolve().parent
def load_config(path: str | Path = "config.yml") -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = (BASE / p).resolve()
    print("Loading", p)
    if not p.exists():
        print("could not find", p)
        return DEFAULTS.copy()

    with p.open("r") as f:
        data = yaml.safe_load(f) or {}
        cfg = DEFAULTS.copy()
        cfg.update({k: v for k, v in data.items() if v is not None})
    return cfg

#global configurables
cfg = load_config()
FLASK_PORT = int(cfg["flask_port"])
ALERT_MESSAGE = str(cfg["alert_message"])
RESOLVED_MESSAGE = str(cfg["resolved_message"])
ALERT_SOUND = str(cfg["alert_sound"])
RESOLVED_SOUND = str(cfg["resolved_sound"])


# ==============================================================================
# INTEGRATED LED CONTROLLER CLASS
# ==============================================================================
class LedController:
    """Manages the Raspberry Pi's ACT and PWR LEDs for alert sequences."""
    def __init__(self):
        self.ACT_LED_DIR = "/sys/class/leds/ACT"  # ACT LED (Green)
        self.PWR_LED_DIR = "/sys/class/leds/PWR"  # PWR LED (Red)
        self.led_thread = None
        self.stop_event = threading.Event()

    def _write_to_led_file(self, led_dir, file_name, value):
        """Helper function to write a value to a specific LED control file."""
        if not os.path.exists(led_dir):
            return
        try:
        
            with open(os.path.join(led_dir, file_name), "w") as f:
                f.write(str(value))
        except Exception as e:
            print(f"Error writing to LED file: {e}. Yo, where is SUDO?")

    def set_leds_to_manual(self):
        """Set both LEDs to 'none' trigger for manual brightness control."""
        self._write_to_led_file(self.ACT_LED_DIR, "trigger", "none")
        self._write_to_led_file(self.PWR_LED_DIR, "trigger", "none")

    def set_solid_green(self):
        """Sets a solid green 'resolved' state."""
        #print("Setting 'Resolved' state: Solid Green LED.")
        # Set trigger to none for manual control
        self._write_to_led_file(self.ACT_LED_DIR, "trigger", "none")
        self._write_to_led_file(self.PWR_LED_DIR, "trigger", "none")
        # Set brightness
        self._write_to_led_file(self.ACT_LED_DIR, "brightness", "1")
        self._write_to_led_file(self.PWR_LED_DIR, "brightness", "0")

    def restore_leds_to_default(self):
        """Restore LEDs to their default Raspberry Pi OS behavior."""
        #print("Restoring LEDs to default system state (ACT=mmc0, PWR=input).")
        self._write_to_led_file(self.ACT_LED_DIR, "trigger", "mmc0")
        self._write_to_led_file(self.PWR_LED_DIR, "trigger", "input")

    def _p1_alert_thread(self):
        """Controls the LED pattern: Double-Flash followed by Alternating Blink."""
        self.set_leds_to_manual()

        # --- PHASE 1: Double-Flash Alert for 10 seconds ---
        #print("--> LED PHASE 1: Double-Flash Alert...")
        start_time = datetime.datetime.now()
        while (datetime.datetime.now() - start_time).total_seconds() < 10:
            if self.stop_event.is_set():
                # If stopped during Phase 1, just clean up and exit
                return
            # Green Double Flash
            for _ in range(2):
                self._write_to_led_file(self.ACT_LED_DIR, "brightness", "1")
                self._write_to_led_file(self.PWR_LED_DIR, "brightness", "0")
                sleep(0.05)
                self._write_to_led_file(self.ACT_LED_DIR, "brightness", "0")
                sleep(0.05)
            sleep(0.3)
            if self.stop_event.is_set(): return

            # Red Double Flash
            for _ in range(2):
                self._write_to_led_file(self.ACT_LED_DIR, "brightness", "0")
                self._write_to_led_file(self.PWR_LED_DIR, "brightness", "1")
                sleep(0.05)
                self._write_to_led_file(self.PWR_LED_DIR, "brightness", "0")
                sleep(0.05)
            sleep(0.3)

        # --- PHASE 2: Alternate blinking until stopped ---
        #print("--> LED PHASE 2: Alternating red and green blink...")
        while not self.stop_event.is_set():
            # Green ON, Red OFF
            self._write_to_led_file(self.ACT_LED_DIR, "brightness", "1")
            self._write_to_led_file(self.PWR_LED_DIR, "brightness", "0")
            sleep(0.15)

            if self.stop_event.is_set(): break

            # Green OFF, Red ON
            self._write_to_led_file(self.ACT_LED_DIR, "brightness", "0")
            self._write_to_led_file(self.PWR_LED_DIR, "brightness", "1")
            sleep(0.15)

    def start_p1_alert_pattern(self):
        """Starts the P1 alert sequence in a new thread."""
        if self.led_thread and self.led_thread.is_alive():
            #print("Alert already in progress.")
            return
        self.stop_event.clear()
        self.led_thread = threading.Thread(target=self._p1_alert_thread)
        self.led_thread.start()

    def stop_alert_pattern(self):
        """Signals the blinking thread to stop."""
        if self.led_thread and self.led_thread.is_alive():
            #print("Signaling alert thread to stop...")
            self.stop_event.set()
            self.led_thread.join() # Wait for the thread to finish cleanly

# ====================================================================
# FLASK APP
# ====================================================================
app = Flask(__name__)
event_queue = queue.Queue()

@app.route('/')
def test():
    return(f"SD alert script running on port {FLASK_PORT}")

@app.route('/start_event')
def trigger_event():
    event_queue.put("Start")
    return "Event triggered"

@app.route('/stop_event', methods=['POST', 'GET'])
def stop_trigger_event():
    event_queue.put("Stop")
    return "Stopping banner", 200

@app.route('/alert', methods=['POST'])
def handle_alert_request():
    data = request.json
    try:
        alert_name = data['alerts'][0]['labels']['alertname']
    except:
        alert_name = 'Missing data!'
    try:
        school = re.search(r'{School=(.*?)}', data['alerts'][0]['valueString']).group(1)
    except:
        school = 'Missing Data!'
    try:
        ticket = data['alerts'][0]['values']['B0']
    except:
        ticket = 'Missing data!'

    alert_log = f'''
=========================
Log for: {datetime.datetime.now()}
Alert Name: {alert_name}
School: {school}
Ticket: #{ticket}
=========================
{json.dumps(data, indent=4)}
===========END===========
'''

    desktop_path = os.path.expanduser("/home/dashboard/Desktop/debug.log")
    with open(desktop_path, 'a') as log_file:
        log_file.write(alert_log)
    print(f"Received Alert: \n{alert_log}")

    try:
        pygame.init()
        pygame.mixer.init()
        alert_sound = pygame.mixer.Sound(ALERT_SOUND)
        pygame.mixer.Sound.play(alert_sound)
        pygame.mixer.quit()
    except Exception as e:
        print(f"Sound error: {e}")

    event_queue.put("Start")
    return '', 200


def run_flask():
    print("App running on port", FLASK_PORT)
    serve(app, host='0.0.0.0', port=FLASK_PORT)


class MainWindow(QMainWindow):
    def __init__(self, led_controller):
        super().__init__()
        self.led_controller = led_controller
        self.setWindowTitle("Application Test")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        layout = QVBoxLayout()
        self.label = QLabel("â  Priority 1 Ticket! â ", self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setFont(QFont("Arial", 60))
        layout.addWidget(self.label)
        central_widget = QWidget(self)
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)
        self.central_widget = central_widget
        self.blinking = False
        self.color_state = 0
        self.central_widget.setStyleSheet("background-color: rgba(255, 0, 0, 0);")
        screen = QApplication.primaryScreen()
        screen_rect = screen.availableGeometry()
        screen_width = screen_rect.width()
        self.setFixedSize(screen_width, 200)
        self.move(0, 0)
        self.timer = QTimer()
        self.timer.timeout.connect(self.blink)
        self.event_check_timer = QTimer()
        self.event_check_timer.timeout.connect(self.check_event_queue)
        self.event_check_timer.start(100)
        

    def blink(self):
        if self.blinking:
            if self.color_state == 0:
                self.central_widget.setStyleSheet("background-color: rgba(255, 0, 0, 255);")
                self.color_state = 1
            else:
                self.central_widget.setStyleSheet("background-color: rgba(255, 0, 0, 0);")
                self.color_state = 0

    def start(self):
        self.blinking = True
        self.timer.start(1000)
        self.label.setText(ALERT_MESSAGE)
        self.central_widget.setStyleSheet("background-color: rgba(255, 0, 0, 0);")
        self.show()
        self.led_controller.start_p1_alert_pattern()

    def stop(self):
        self.blinking = False
        self.timer.stop()
        self.central_widget.setStyleSheet("background-color: rgba(0, 255, 0, 255);")
        self.label.setText(RESOLVED_MESSAGE)

        # ===> MODIFIED LED LOGIC FOR STOP EVENT <===
        # 1. Stop the blinking alert pattern thread.
        self.led_controller.stop_alert_pattern()
        # 2. Immediately set the solid green "resolved" state.
        self.led_controller.set_solid_green()
        try:
            pygame.mixer.quit()
            pygame.mixer.init()
            resolved_sound = pygame.mixer.Sound(RESOLVED_SOUND)
            pygame.mixer.Sound.play(resolved_sound)
        except Exception as e:
            print(f"Sound error: {e}")

        # 3. After 10 seconds, hide the banner AND restore default LED behavior.
        # A lambda is used to call two functions from one timer signal.
        QTimer.singleShot(10000, lambda: [self.hide(), self.led_controller.restore_leds_to_default()])

    def check_event_queue(self):
        while not event_queue.empty():
            event = event_queue.get()
            print("Event Queue not empty. Contains: {0}".format(event))
            if event == "Start":
                self.start()
                event_queue.queue.clear()
            elif event == "Stop":
                self.stop()
                event_queue.queue.clear()

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Permission denied. Please run this script with 'sudo'.")
        exit()

    led_controller = LedController()
    led_controller.restore_leds_to_default() # Ensure a clean state on startup

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    app = QApplication(sys.argv)
    window = MainWindow(led_controller)
    
    # When the GUI is about to quit, make sure LEDs are back to normal
    app.aboutToQuit.connect(led_controller.restore_leds_to_default)
    
    sys.exit(app.exec_())

