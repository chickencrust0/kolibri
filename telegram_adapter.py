"""Telegram transport for the shared Kolibri business handlers.

The handlers use the small MAX-shaped transport interface from the MAX bot;
this adapter keeps that layer messenger-neutral while using aiogram for I/O.
"""
from typing import Any
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

def _markup(attachments):
    if not attachments: return None
    buttons=[]
    request=False
    for block in attachments:
        if block.get("type") != "inline_keyboard": continue
        for row in (block.get("payload") or {}).get("buttons", []):
            out=[]
            for b in row:
                typ=b.get("type")
                if typ == "request_contact": request=True; out.append(KeyboardButton(text=b.get("text","Телефон"), request_contact=True))
                elif typ == "callback": out.append(InlineKeyboardButton(text=b.get("text",""), callback_data=b.get("payload","")))
                elif typ == "link": out.append(InlineKeyboardButton(text=b.get("text",""), url=b.get("url","")))
                elif typ == "message": out.append(InlineKeyboardButton(text=b.get("text",""), callback_data="menu:start"))
            if out: buttons.append(out)
    if request: return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

class TelegramBot:
    def __init__(self, token: str):
        self._bot=Bot(token); self._messages={}; self._callbacks={}
    async def get_me(self):
        u=await self._bot.get_me(); return {"name":u.full_name,"username":u.username}
    async def close(self): await self._bot.session.close()
    async def send_message(self, user_id=None, text="", attachments=None, fmt=None, **kwargs):
        media = next((a for a in (attachments or []) if a.get("type") in ("image", "file")), None)
        if media:
            token=(media.get("payload") or {}).get("token")
            if media["type"] == "image": m=await self._bot.send_photo(user_id, token, caption=text or None, parse_mode="HTML" if fmt else None)
            else: m=await self._bot.send_document(user_id, token, caption=text or None, parse_mode="HTML" if fmt else None)
        else:
            m=await self._bot.send_message(user_id, text, reply_markup=_markup(attachments), parse_mode="HTML" if fmt else None)
        mid=f"{m.chat.id}:{m.message_id}"
        self._messages[mid]=m.chat.id
        return {"message":{"body":{"mid":mid}}}
    async def edit_message(self, message_id, text, attachments=None, fmt=None, **kwargs):
        raw=str(message_id); chat_id=self._messages.get(raw)
        msg_id=raw.rsplit(":", 1)[-1]
        if chat_id:
            await self._bot.edit_message_text(text, chat_id, int(msg_id), reply_markup=_markup(attachments), parse_mode="HTML" if fmt else None)
        return {}
    async def answer_callback(self, callback_id, notification=None):
        await self._bot.answer_callback_query(callback_id, text=notification)

def telegram_update(bot, update):
    if update.message:
        m=update.message; sender=m.from_user
        mid=f"{m.chat.id}:{m.message_id}"
        body={"mid":mid,"text":m.text or m.caption or "","attachments":[]}
        bot._messages[mid]=m.chat.id
        if m.contact: body["attachments"].append({"type":"contact","payload":{"phone":m.contact.phone_number,"max_info":{"user_id":m.contact.user_id}}})
        if m.photo: body["attachments"].append({"type":"image","payload":{"token":m.photo[-1].file_id}})
        if m.document: body["attachments"].append({"type":"file","payload":{"token":m.document.file_id,"filename":m.document.file_name or "file"}})
        return {"update_type":"message_created","message":{"sender":{"user_id":sender.id,"first_name":sender.first_name,"last_name":sender.last_name or "","username":sender.username},"body":body}}
    if update.callback_query:
        c=update.callback_query; u=c.from_user; m=c.message
        mid=f"{m.chat.id}:{m.message_id}"
        bot._callbacks[c.id]=c; bot._messages[mid]=m.chat.id
        return {"update_type":"message_callback","callback":{"callback_id":c.id,"payload":c.data or "","user":{"user_id":u.id,"first_name":u.first_name,"last_name":u.last_name or "","username":u.username}},"message":{"body":{"mid":mid,"text":m.text or ""}}}
