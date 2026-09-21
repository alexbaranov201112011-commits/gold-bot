(bot.send_message(chat_id=chat_id, text=f"Ошибка анализа: {e}"))
        except: pass
    return 'ok'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
