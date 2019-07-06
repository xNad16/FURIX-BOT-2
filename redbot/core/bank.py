import asyncio
import datetime
from typing import Union, List, Optional
from functools import wraps

import discord

from . import Config, errors, commands
from .i18n import Translator

_ = Translator("Bank API", __file__)

__all__ = [
    "MAX_BALANCE",
    "Account",
    "get_balance",
    "set_balance",
    "withdraw_credits",
    "deposit_credits",
    "can_spend",
    "transfer_credits",
    "wipe_bank",
    "get_account",
    "is_global",
    "set_global",
    "get_bank_name",
    "set_bank_name",
    "get_currency_name",
    "set_currency_name",
    "get_default_balance",
    "set_default_balance",
    "cost",
    "AbortPurchase",
]

MAX_BALANCE = 2 ** 63 - 1

_DEFAULT_GLOBAL = {
    "is_global": False,
    "bank_name": "Twentysix bank",
    "currency": "credits",
    "default_balance": 100,
}

_DEFAULT_GUILD = {"bank_name": "Twentysix bank", "currency": "credits", "default_balance": 100}

_DEFAULT_MEMBER = {"name": "", "balance": 0, "created_at": 0}

_DEFAULT_USER = _DEFAULT_MEMBER

_conf: Config = None


def _init():
    global _conf
    _conf = Config.get_conf(None, 384734293238749, cog_name="Bank", force_registration=True)
    _conf.register_global(**_DEFAULT_GLOBAL)
    _conf.register_guild(**_DEFAULT_GUILD)
    _conf.register_member(**_DEFAULT_MEMBER)
    _conf.register_user(**_DEFAULT_USER)


class Account:
    """A single account.

    This class should ONLY be instantiated by the bank itself."""

    def __init__(self, name: str, balance: int, created_at: datetime.datetime):
        self.name = name
        self.balance = balance
        self.created_at = created_at


def _encoded_current_time() -> int:
    """Get the current UTC time as a timestamp.
    
    Returns
    -------
    int
        The current UTC timestamp.

    """
    now = datetime.datetime.utcnow()
    return _encode_time(now)


def _encode_time(time: datetime.datetime) -> int:
    """Convert a datetime object to a serializable int.
    
    Parameters
    ----------
    time : datetime.datetime
        The datetime to convert.
        
    Returns
    -------
    int
        The timestamp of the datetime object.

    """
    ret = int(time.timestamp())
    return ret


def _decode_time(time: int) -> datetime.datetime:
    """Convert a timestamp to a datetime object.
    
    Parameters
    ----------
    time : int
        The timestamp to decode.
        
    Returns
    -------
    datetime.datetime
        The datetime object from the timestamp.

    """
    return datetime.datetime.utcfromtimestamp(time)


async def get_balance(member: discord.Member) -> int:
    """Get the current balance of a member.

    Parameters
    ----------
    member : discord.Member
        The member whose balance to check.

    Returns
    -------
    int
        The member's balance

    """
    acc = await get_account(member)
    return acc.balance


async def can_spend(member: discord.Member, amount: int) -> bool:
    """Determine if a member can spend the given amount.

    Parameters
    ----------
    member : discord.Member
        The member wanting to spend.
    amount : int
        The amount the member wants to spend.

    Returns
    -------
    bool
        :code:`True` if the member has a sufficient balance to spend the
        amount, else :code:`False`.

    """
    if _invalid_amount(amount):
        return False
    return await get_balance(member) >= amount


async def set_balance(member: discord.Member, amount: int) -> int:
    """Set an account balance.

    Parameters
    ----------
    member : discord.Member
        The member whose balance to set.
    amount : int
        The amount to set the balance to.

    Returns
    -------
    int
        New account balance.

    Raises
    ------
    ValueError
        If attempting to set the balance to a negative number.
    BalanceTooHigh
        If attempting to set the balance to a value greater than
        ``bank.MAX_BALANCE``

    """
    if amount < 0:
        raise ValueError("Not allowed to have negative balance.")
    if amount > MAX_BALANCE:
        currency = (
            await get_currency_name()
            if await is_global()
            else await get_currency_name(member.guild)
        )
        raise errors.BalanceTooHigh(
            user=member.display_name, max_balance=MAX_BALANCE, currency_name=currency
        )
    if await is_global():
        group = _conf.user(member)
    else:
        group = _conf.member(member)
    await group.balance.set(amount)

    if await group.created_at() == 0:
        time = _encoded_current_time()
        await group.created_at.set(time)

    if await group.name() == "":
        await group.name.set(member.display_name)

    return amount


def _invalid_amount(amount: int) -> bool:
    return amount < 0


async def withdraw_credits(member: discord.Member, amount: int) -> int:
    """Remove a certain amount of credits from an account.

    Parameters
    ----------
    member : discord.Member
        The member to withdraw credits from.
    amount : int
        The amount to withdraw.

    Returns
    -------
    int
        New account balance.

    Raises
    ------
    ValueError
        If the withdrawal amount is invalid or if the account has insufficient
        funds.
    TypeError
        If the withdrawal amount is not an `int`.

    """
    if not isinstance(amount, int):
        raise TypeError("Withdrawal amount must be of type int, not {}.".format(type(amount)))
    if _invalid_amount(amount):
        raise ValueError("Invalid withdrawal amount {} < 0".format(amount))

    bal = await get_balance(member)
    if amount > bal:
        raise ValueError("Insufficient funds {} > {}".format(amount, bal))

    return await set_balance(member, bal - amount)


async def deposit_credits(member: discord.Member, amount: int) -> int:
    """Add a given amount of credits to an account.

    Parameters
    ----------
    member : discord.Member
        The member to deposit credits to.
    amount : int
        The amount to deposit.

    Returns
    -------
    int
        The new balance.

    Raises
    ------
    ValueError
        If the deposit amount is invalid.
    TypeError
        If the deposit amount is not an `int`.

    """
    if not isinstance(amount, int):
        raise TypeError("Deposit amount must be of type int, not {}.".format(type(amount)))
    if _invalid_amount(amount):
        raise ValueError("Invalid deposit amount {} <= 0".format(amount))

    bal = await get_balance(member)
    return await set_balance(member, amount + bal)


async def transfer_credits(from_: discord.Member, to: discord.Member, amount: int):
    """Transfer a given amount of credits from one account to another.

    Parameters
    ----------
    from_: discord.Member
        The member to transfer from.
    to : discord.Member
        The member to transfer to.
    amount : int
        The amount to transfer.

    Returns
    -------
    int
        The new balance of the member gaining credits.

    Raises
    ------
    ValueError
        If the amount is invalid or if ``from_`` has insufficient funds.
    TypeError
        If the amount is not an `int`.

    """
    if not isinstance(amount, int):
        raise TypeError("Transfer amount must be of type int, not {}.".format(type(amount)))
    if _invalid_amount(amount):
        raise ValueError("Invalid transfer amount {} <= 0".format(amount))

    await withdraw_credits(from_, amount)
    return await deposit_credits(to, amount)


async def wipe_bank(guild: Optional[discord.Guild] = None) -> None:
    """Delete all accounts from the bank.

    Parameters
    ----------
    guild : discord.Guild
        The guild to clear accounts for. If unsupplied and the bank is
        per-server, all accounts in every guild will be wiped.

    """
    if await is_global():
        await _conf.clear_all_users()
    else:
        await _conf.clear_all_members(guild)


async def get_leaderboard(positions: int = None, guild: discord.Guild = None) -> List[tuple]:
    """
    Gets the bank's leaderboard

    Parameters
    ----------
    positions : `int`
        The number of positions to get
    guild : discord.Guild
        The guild to get the leaderboard of. If the bank is global and this
        is provided, get only guild members on the leaderboard

    Returns
    -------
    `list` of `tuple`
        The sorted leaderboard in the form of :code:`(user_id, raw_account)`

    Raises
    ------
    TypeError
        If the bank is guild-specific and no guild was specified

    """
    if await is_global():
        raw_accounts = await _conf.all_users()
        if guild is not None:
            tmp = raw_accounts.copy()
            for acc in tmp:
                if not guild.get_member(acc):
                    del raw_accounts[acc]
    else:
        if guild is None:
            raise TypeError("Expected a guild, got NoneType object instead!")
        raw_accounts = await _conf.all_members(guild)
    sorted_acc = sorted(raw_accounts.items(), key=lambda x: x[1]["balance"], reverse=True)
    if positions is None:
        return sorted_acc
    else:
        return sorted_acc[:positions]


async def get_leaderboard_position(
    member: Union[discord.User, discord.Member]
) -> Union[int, None]:
    """
    Get the leaderboard position for the specified user

    Parameters
    ----------
    member : `discord.User` or `discord.Member`
        The user to get the leaderboard position of

    Returns
    -------
    `int`
        The position of the user on the leaderboard

    Raises
    ------
    TypeError
        If the bank is currently guild-specific and a `discord.User` object was passed in

    """
    if await is_global():
        guild = None
    else:
        guild = member.guild if hasattr(member, "guild") else None
    try:
        leaderboard = await get_leaderboard(None, guild)
    except TypeError:
        raise
    else:
        pos = discord.utils.find(lambda x: x[1][0] == member.id, enumerate(leaderboard, 1))
        if pos is None:
            return None
        else:
            return pos[0]


async def get_account(member: Union[discord.Member, discord.User]) -> Account:
    """Get the appropriate account for the given user or member.

    A member is required if the bank is currently guild specific.

    Parameters
    ----------
    member : `discord.User` or `discord.Member`
        The user whose account to get.

    Returns
    -------
    Account
        The user's account.

    """
    if await is_global():
        all_accounts = await _conf.all_users()
    else:
        all_accounts = await _conf.all_members(member.guild)

    if member.id not in all_accounts:
        acc_data = {"name": member.display_name, "created_at": _DEFAULT_MEMBER["created_at"]}
        try:
            acc_data["balance"] = await get_default_balance(member.guild)
        except AttributeError:
            acc_data["balance"] = await get_default_balance()
    else:
        acc_data = all_accounts[member.id]

    acc_data["created_at"] = _decode_time(acc_data["created_at"])
    return Account(**acc_data)


async def is_global() -> bool:
    """Determine if the bank is currently global.

    Returns
    -------
    bool
        :code:`True` if the bank is global, otherwise :code:`False`.

    """
    return await _conf.is_global()


async def set_global(global_: bool) -> bool:
    """Set global status of the bank.

    .. important::

        All accounts are reset when you switch!

    Parameters
    ----------
    global_ : bool
        :code:`True` will set bank to global mode.

    Returns
    -------
    bool
        New bank mode, :code:`True` is global.

    Raises
    ------
    RuntimeError
        If bank is becoming global and a `discord.Member` was not provided.

    """
    if (await is_global()) is global_:
        return global_

    if await is_global():
        await _conf.clear_all_users()
    else:
        await _conf.clear_all_members()

    await _conf.is_global.set(global_)
    return global_


async def get_bank_name(guild: discord.Guild = None) -> str:
    """Get the current bank name.

    Parameters
    ----------
    guild : `discord.Guild`, optional
        The guild to get the bank name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The bank's name.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        return await _conf.bank_name()
    elif guild is not None:
        return await _conf.guild(guild).bank_name()
    else:
        raise RuntimeError("Guild parameter is required and missing.")


async def set_bank_name(name: str, guild: discord.Guild = None) -> str:
    """Set the bank name.

    Parameters
    ----------
    name : str
        The new name for the bank.
    guild : `discord.Guild`, optional
        The guild to set the bank name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The new name for the bank.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        await _conf.bank_name.set(name)
    elif guild is not None:
        await _conf.guild(guild).bank_name.set(name)
    else:
        raise RuntimeError("Guild must be provided if setting the name of a guild-specific bank.")
    return name


async def get_currency_name(guild: discord.Guild = None) -> str:
    """Get the currency name of the bank.

    Parameters
    ----------
    guild : `discord.Guild`, optional
        The guild to get the currency name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The currency name.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        return await _conf.currency()
    elif guild is not None:
        return await _conf.guild(guild).currency()
    else:
        raise RuntimeError("Guild must be provided.")


async def set_currency_name(name: str, guild: discord.Guild = None) -> str:
    """Set the currency name for the bank.

    Parameters
    ----------
    name : str
        The new name for the currency.
    guild : `discord.Guild`, optional
        The guild to set the currency name for (required if bank is
        guild-specific).

    Returns
    -------
    str
        The new name for the currency.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        await _conf.currency.set(name)
    elif guild is not None:
        await _conf.guild(guild).currency.set(name)
    else:
        raise RuntimeError(
            "Guild must be provided if setting the currency name of a guild-specific bank."
        )
    return name


async def get_default_balance(guild: discord.Guild = None) -> int:
    """Get the current default balance amount.

    Parameters
    ----------
    guild : `discord.Guild`, optional
        The guild to get the default balance for (required if bank is
        guild-specific).

    Returns
    -------
    int
        The bank's default balance.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.

    """
    if await is_global():
        return await _conf.default_balance()
    elif guild is not None:
        return await _conf.guild(guild).default_balance()
    else:
        raise RuntimeError("Guild is missing and required!")


async def set_default_balance(amount: int, guild: discord.Guild = None) -> int:
    """Set the default balance amount.

    Parameters
    ----------
    amount : int
        The new default balance.
    guild : `discord.Guild`, optional
        The guild to set the default balance for (required if bank is
        guild-specific).

    Returns
    -------
    int
        The new default balance.

    Raises
    ------
    RuntimeError
        If the bank is guild-specific and guild was not provided.
    ValueError
        If the amount is invalid.

    """
    amount = int(amount)
    if amount < 0:
        raise ValueError("Amount must be greater than zero.")

    if await is_global():
        await _conf.default_balance.set(amount)
    elif guild is not None:
        await _conf.guild(guild).default_balance.set(amount)
    else:
        raise RuntimeError("Guild is missing and required.")

    return amount


class AbortPurchase(Exception):
    pass


def cost(amount: int):
    """
    Decorates a coroutine-function or command to have a cost.

    If the command raises an exception, the cost will be refunded.

    You can intentionally refund by raising `AbortPurchase`
    (this error will be consumed and not show to users)

    Other exceptions will propogate and will be handled by Red's (and/or
    any other configured) error handling.
    """
    if not isinstance(amount, int) or amount < 0:
        raise ValueError("This decorator requires an integer cost greater than or equal to zero")

    def deco(coro_or_command):
        is_command = isinstance(coro_or_command, commands.Command)
        if not is_command and not asyncio.iscoroutinefunction(coro_or_command):
            raise TypeError("@bank.cost() can only be used on commands or `async def` functions")

        coro = coro_or_command.callback if is_command else coro_or_command

        @wraps(coro)
        async def wrapped(*args, **kwargs):
            context: commands.Context = None
            for arg in args:
                if isinstance(arg, commands.Context):
                    context = arg
                    break

            if not context.guild and not await is_global():
                raise commands.UserFeedbackCheckFailure(
                    _("Can't pay for this command in DM without a global bank.")
                )
            try:
                await withdraw_credits(context.author, amount)
            except Exception:
                credits_name = await get_currency_name(context.guild)
                raise commands.UserFeedbackCheckFailure(
                    _("You need at least {cost} {currency} to use this command.").format(
                        cost=amount, currency=credits_name
                    )
                )
            else:
                try:
                    return await coro(*args, **kwargs)
                except AbortPurchase:
                    await deposit_credits(context.author, amount)
                except Exception:
                    await deposit_credits(context.author, amount)
                    raise

        if not is_command:
            return wrapped
        else:
            wrapped.__module__ = coro_or_command.callback.__module__
            coro_or_command.callback = wrapped
            return coro_or_command

    return deco
