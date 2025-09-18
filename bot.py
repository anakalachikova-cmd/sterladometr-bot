# webhook.py
from flask import Flask
from threading import Thread
from bot import main
import time

app = Flask('')

@app.route('/')
def home():
    return "Alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

if __name__ == '__main__':
    time.sleep(5)  # Ждём 5 секунд перед запуском
    keep_alive()
    main()
