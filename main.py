from datetime import time as dtime
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters)

import handlers as h
import autopilot
from config import TELEGRAM_TOKEN, SCAN_INTERVAL_S, SEND_INTERVAL_S, DIGEST_HOUR, TZ, log


def main():
    app = (Application.builder().token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(30)
        .get_updates_connect_timeout(20).get_updates_read_timeout(40)
        .post_init(h.setup_bot)
        .build())

    # ---- commands
    app.add_handler(CommandHandler("start", h.start))
    app.add_handler(CommandHandler("profile", h.profile_cmd))
    app.add_handler(CommandHandler("location", h.location_cmd))
    app.add_handler(CommandHandler("search", h.search_cmd))
    app.add_handler(CommandHandler("note", h.note_cmd))
    app.add_handler(CommandHandler("auto", h.auto_cmd))
    app.add_handler(CommandHandler("history", h.history_cmd))
    app.add_handler(CommandHandler("delete", h.delete_cmd))
    app.add_handler(CommandHandler("sender", h.sender_cmd))
    app.add_handler(CommandHandler("stats", h.stats_cmd))
    app.add_handler(CommandHandler("admin", h.admin_cmd))
    app.add_handler(CommandHandler("sheet", h.sheet_cmd))# main.py
    app.add_handler(CommandHandler("run", h.run_cmd))
    app.add_handler(CallbackQueryHandler(h.on_run, pattern=r"^run:"))

    # ---- buttons: every handler has its own pattern, so none of them swallows another's callbacks
    app.add_handler(CallbackQueryHandler(h.on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(h.on_location_pick, pattern=r"^loc:"))
    app.add_handler(CallbackQueryHandler(h.on_auto_set, pattern=r"^autoset:"))
    app.add_handler(CallbackQueryHandler(h.on_admin_button, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(
        h.on_button, pattern=r"^(apply|skip|send|regen|cancel|done|autoconsent|delete):"))

    # ---- CV upload
    app.add_handler(MessageHandler(filters.Document.ALL, h.on_document))
    app.add_error_handler(h.on_error)

    # ---- scheduled jobs
    app.job_queue.run_repeating(autopilot.scan_all, interval=SCAN_INTERVAL_S, first=60, name="scan_all")
    app.job_queue.run_repeating(autopilot.send_due, interval=SEND_INTERVAL_S, first=30, name="send_due")
    app.job_queue.run_daily(autopilot.daily_digest, time=dtime(DIGEST_HOUR, 0, tzinfo=TZ), name="digest")

    log.info("Bot started, polling...")
    app.run_polling(bootstrap_retries=-1)   


if __name__ == "__main__":
    main()