"""Секретарь: организация рабочего дня — где человек находится, рабочие сессии и перерывы с таймером.

parse.py — фразы правилами; places.py — город, страна, часовой пояс; weather.py — погода сейчас (Yandex Search
API + модель AI Studio); store.py — PostgreSQL; service.py — действия и ответы; timer.py — поток таймера в демоне.
Наружу уходят только запросы к Yandex Cloud (AI Studio и Search API).
"""
