import secrets
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # chat_id Telegram
    title: Mapped[str] = mapped_column(String)
    invite_code: Mapped[str] = mapped_column(
        String, unique=True, default=lambda: secrets.token_hex(8)
    )
    bot_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # user_id Telegram
    first_name: Mapped[str] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    username: Mapped[str | None] = mapped_column(String)
    photo_url: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"))
    title: Mapped[str] = mapped_column(String)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    participants: Mapped[list["EventParticipant"]] = relationship(
        cascade="all, delete-orphan"
    )
    expenses: Mapped[list["Expense"]] = relationship(cascade="all, delete-orphan")
    settlements: Mapped[list["Settlement"]] = relationship(cascade="all, delete-orphan")


class EventParticipant(Base):
    __tablename__ = "event_participants"

    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)

    user: Mapped[User] = relationship()


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    title: Mapped[str] = mapped_column(String)
    payer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    tip_percent: Mapped[int] = mapped_column(Integer, default=0)
    photo_file_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")  # open / closed
    leftover_mode: Mapped[str | None] = mapped_column(String)  # split / payer, пока не закрыт — None
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    payer: Mapped[User] = relationship(foreign_keys=[payer_id])
    author: Mapped[User] = relationship(foreign_keys=[created_by])
    items: Mapped[list["Item"]] = relationship(cascade="all, delete-orphan")


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"))
    name: Mapped[str] = mapped_column(String)
    unit_price: Mapped[int] = mapped_column(Integer)  # копейки
    qty: Mapped[int] = mapped_column(Integer, default=1)
    for_all: Mapped[bool] = mapped_column(Boolean, default=False)

    claims: Mapped[list["Claim"]] = relationship(cascade="all, delete-orphan")


class Claim(Base):
    __tablename__ = "claims"

    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    units: Mapped[int] = mapped_column(Integer, default=1)

    user: Mapped[User] = relationship()


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    from_user: Mapped[int] = mapped_column(ForeignKey("users.id"))
    to_user: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[int] = mapped_column(Integer)  # копейки
    status: Mapped[str] = mapped_column(String, default="pending")  # pending / confirmed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
