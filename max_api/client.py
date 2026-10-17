"""Мост к транспорту: хендлеры работают с интерфейсом MaxBot.

В версии для MAX здесь лежал HTTP-клиент MAX; в Telegram-версии тот же
интерфейс реализует telegram_adapter.TelegramBot, а сюда импортируется
алиас — имена MaxBot/MaxAPIError оставлены, чтобы общий слой
(bot/dispatcher.py, хендлеры) не пришлось переписывать.
"""
from telegram_adapter import TelegramBot as MaxBot

class MaxAPIError(RuntimeError):
    pass
