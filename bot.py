def auto_send_signal():
    last_candle_time = None
    last_signal = None

    while True:
        try:
            m15 = get_candles("15min")

            if m15 is None or len(m15) < 10:
                time.sleep(60)
                continue

            closed = closed_candles(m15)

            if closed is None or len(closed) < 2:
                time.sleep(60)
                continue

            current_candle_time = str(
                closed["datetime"].iloc[-1]
            )

            # Анализируем только новую закрытую M15 свечу
            if current_candle_time != last_candle_time:

                last_candle_time = current_candle_time

                analysis = analyze()

                signal = analysis.get("signal")

                if signal in [
                    "BUY",
                    "SELL",
                    "EARLY BUY",
                    "EARLY SELL"
                ]:

                    # Не повторяем одинаковый сигнал
                    signal_key = (
                        current_candle_time,
                        signal
                    )

                    if signal_key != last_signal:

                        message = format_signal(
                            analysis
                        )

                        telegram_send(
                            TELEGRAM_CHAT_ID,
                            message
                        )

                        last_signal = signal_key

                        print(
                            "AUTO SIGNAL:",
                            signal
                        )

                else:

                    print(
                        "AUTO:",
                        signal,
                        "| candle:",
                        current_candle_time
                    )

        except Exception as e:

            print(
                "Auto signal error:",
                e
            )

        time.sleep(60)
