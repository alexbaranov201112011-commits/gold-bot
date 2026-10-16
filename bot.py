GOLD SMART V7.8 — PATCH ДЛЯ АВТОМАТИЧЕСКОГО ЦИКЛА И TELEGRAM
Дата: 2026-10-16

ВАЖНО:
GitHub не разрешил записать изменения автоматически (HTTP 403), поэтому этот патч не меняет репозиторий сам.
Перед редактированием сохраните копию bot.py.
Выполните замены ниже в GitHub → bot.py → Edit (карандаш), затем Commit changes.
Не меняйте AUTO_TRADING=False.

1) В fresh_state() добавьте поля в словарь рядом с last_success/last_error:
        "worker_heartbeat": None, "last_cycle_started": None, "last_cycle_finished": None,
        "last_telegram_send": None, "last_telegram_error": None, "last_heartbeat_message": None,

2) Полностью замените функцию tg_send() на эту:

def tg_send(message: str, parse_mode: Optional[str] = "HTML") -> bool:
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID:
        state["last_telegram_error"] = "Не заданы BOT_TOKEN или TELEGRAM_CHAT_ID"
        return False
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=TIMEOUT
        )
        data = r.json()
        if r.ok and bool(data.get("ok")):
            state["last_telegram_send"] = iso_now()
            state["last_telegram_error"] = None
            return True
        state["last_telegram_error"] = (
            f"Telegram HTTP {r.status_code}: "
            f"{data.get('description', 'sendMessage failed')}"
        )
        return False
    except Exception as e:
        state["last_telegram_error"] = f"{type(e).__name__}: {e}"
        return False

3) В cycle_once() замените первые две строки:
    reset_day(); state["last_cycle"]=iso_now(); price,raw,dt,real=fetch_market(); a=analyze(price,raw,real)

на:
    reset_day()
    state["last_cycle"] = iso_now()
    state["last_cycle_started"] = state["last_cycle"]
    state["worker_heartbeat"] = state["last_cycle"]
    price, raw, dt, real = fetch_market()
    a = analyze(price, raw, real)

4) В конце cycle_once(), замените последние две строки:
    save_state()
def worker_loop():

на:
    state["last_cycle_finished"] = iso_now()
    state["worker_heartbeat"] = state["last_cycle_finished"]
    save_state()

def worker_loop():

5) Полностью замените worker_loop() и start_background() на этот блок:

def worker_loop():
    # Одно стартовое сообщение подтверждает, что автоматический поток запустился.
    tg_send(
        f"🟢 <b>GOLD SMART V{VERSION}: автоматический цикл запущен</b>\n"
        f"Опрос рынка каждые {POLL_SECONDS} сек.\n"
        "Режим: виртуальный · AUTO_TRADING=OFF"
    )
    last_heartbeat_notice = time.time()

    while not stop_event.is_set():
        try:
            with lock:
                cycle_once()
        except Exception as e:
            with lock:
                state["last_error"] = f"{type(e).__name__}: {e}"
                state["last_cycle"] = iso_now()
                state["last_cycle_finished"] = iso_now()
                state["worker_heartbeat"] = iso_now()
                save_state()

        # Контрольное сообщение раз в час, даже если сетапа нет.
        if time.time() - last_heartbeat_notice >= 3600:
            p = state.get("last_price")
            p_text = f"{float(p):.2f}" if isinstance(p, (int, float)) else "нет данных"
            ok = tg_send(
                f"🟢 <b>GOLD SMART V{VERSION}: бот работает</b>\n"
                f"Цена: {p_text}\n"
                f"Последний успешный анализ: {state.get('last_success') or 'пока нет'}\n"
                "Сигнала нет или условия не подтверждены.\nAUTO_TRADING=OFF"
            )
            if ok:
                state["last_heartbeat_message"] = iso_now()
                save_state()
            last_heartbeat_notice = time.time()

        stop_event.wait(POLL_SECONDS)


worker_thread = None
command_thread = None
background_lock = threading.Lock()


def start_background():
    # Если поток завершился, следующий HTTP-запрос запустит его снова.
    global worker_thread, command_thread
    with background_lock:
        if worker_thread is None or not worker_thread.is_alive():
            worker_thread = threading.Thread(
                target=worker_loop, name="gold-smart-worker", daemon=True
            )
            worker_thread.start()
        if command_thread is None or not command_thread.is_alive():
            command_thread = threading.Thread(
                target=poll_commands, name="gold-smart-telegram", daemon=True
            )
            command_thread.start()

6) Полностью замените status_text() на этот вариант:

def status_text() -> str:
    p = state.get("last_price")
    t = state.get("open_trade")
    return "\n".join([
        f"🥇 <b>GOLD SMART V{VERSION}</b>",
        "Engine: ON",
        f"Последний успешный анализ: {state.get('last_success') or 'ещё не было'}",
        f"Heartbeat потока: {state.get('worker_heartbeat') or 'нет'}",
        f"Цикл начат: {state.get('last_cycle_started') or 'нет'}",
        f"Цикл завершён: {state.get('last_cycle_finished') or 'нет'}",
        f"Цена: {p:.2f}" if isinstance(p, (int, float)) else "Цена: нет данных",
        f"Данные: {state.get('last_data_time') or 'нет'}",
        f"Ошибка анализа: {state.get('last_error') or 'нет'}",
        f"Последняя отправка Telegram: {state.get('last_telegram_send') or 'нет'}",
        f"Ошибка Telegram: {state.get('last_telegram_error') or 'нет'}",
        f"Последнее контрольное сообщение: {state.get('last_heartbeat_message') or 'нет'}",
        f"FULL сигналов всего: {state.get('signal_count', 0)}",
        f"Виртуальная сделка: {t['direction']} @ {t['entry']:.2f}" if t else "Виртуальная сделка: нет",
        "AUTO_TRADING: OFF"
    ])

7) В web_status() добавьте эти поля в возвращаемый JSON:
        "last_cycle_started": state.get("last_cycle_started"),
        "last_cycle_finished": state.get("last_cycle_finished"),
        "worker_heartbeat": state.get("worker_heartbeat"),
        "last_telegram_send": state.get("last_telegram_send"),
        "last_telegram_error": state.get("last_telegram_error"),
        "last_heartbeat_message": state.get("last_heartbeat_message"),

ПРОВЕРКА ПОСЛЕ DEPLOY:
1. Откройте https://gold-bot-q8la.onrender.com/status
2. Убедитесь, что версия V7.8 и last_cycle/last_success обновляются примерно раз в минуту.
3. В Telegram должно прийти сообщение «автоматический цикл запущен».
4. Если отправка не работает, поле last_telegram_error покажет причину.
5. Примерно через час придёт контрольное сообщение даже без сигнала.

ОГРАНИЧЕНИЯ:
- Сообщения о запуске/контроле не являются торговыми сигналами.
- При отсутствии подтверждённого сетапа торгового сообщения не будет.
- XAUS сейчас отдаёт точки цены без настоящих OHLC (real_ohlc=false), поэтому свечная структура приблизительная.
- AUTO_TRADING остаётся выключенным; код не должен размещать реальные ордера.
