import asyncio
import inspect
import os
import logging
from collections import Counter
from enum import Enum
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Optional, Union, List

import discord
import sys
from discord.ext.commands import when_mentioned_or

from . import Config, i18n, commands, errors
from .cog_manager import CogManager

from .rpc import RPCMixin
from .utils import common_filters

CUSTOM_GROUPS = "CUSTOM_GROUPS"

log = logging.getLogger("redbot")


def _is_submodule(parent, child):
    return parent == child or child.startswith(parent + ".")


# barely spurious warning caused by our intentional shadowing
class RedBase(commands.GroupMixin, commands.bot.BotBase, RPCMixin):  # pylint: disable=no-member
    """Mixin for the main bot class.

    This exists because `Red` inherits from `discord.AutoShardedClient`, which
    is something other bot classes may not want to have as a parent class.
    """

    def __init__(self, *args, cli_flags=None, bot_dir: Path = Path.cwd(), **kwargs):
        self._shutdown_mode = ExitCodes.CRITICAL
        self.db = Config.get_core_conf(force_registration=True)
        self._co_owners = cli_flags.co_owner
        self.rpc_enabled = cli_flags.rpc
        self._last_exception = None
        self.db.register_global(
            token=None,
            prefix=[],
            packages=[],
            owner=None,
            whitelist=[],
            blacklist=[],
            locale="en-US",
            embeds=True,
            color=15158332,
            fuzzy=False,
            custom_info=None,
            help__page_char_limit=1000,
            help__max_pages_in_guild=2,
            help__use_menus=False,
            help__show_hidden=False,
            help__verify_checks=True,
            help__verify_exists=False,
            help__tagline="",
            invite_public=False,
            invite_perm=0,
            disabled_commands=[],
            disabled_command_msg="That command is disabled.",
            api_tokens={},
            extra_owner_destinations=[],
            owner_opt_out_list=[],
            schema_version=0,
        )

        self.db.register_guild(
            prefix=[],
            whitelist=[],
            blacklist=[],
            admin_role=[],
            mod_role=[],
            embeds=None,
            use_bot_color=False,
            fuzzy=False,
            disabled_commands=[],
            autoimmune_ids=[],
        )

        self.db.register_user(embeds=None)

        self.db.init_custom(CUSTOM_GROUPS, 2)
        self.db.register_custom(CUSTOM_GROUPS)

        async def prefix_manager(bot, message):
            if not cli_flags.prefix:
                global_prefix = await bot.db.prefix()
            else:
                global_prefix = cli_flags.prefix
            if message.guild is None:
                return global_prefix
            server_prefix = await bot.db.guild(message.guild).prefix()
            if cli_flags.mentionable:
                return (
                    when_mentioned_or(*server_prefix)(bot, message)
                    if server_prefix
                    else when_mentioned_or(*global_prefix)(bot, message)
                )
            else:
                return server_prefix if server_prefix else global_prefix

        if "command_prefix" not in kwargs:
            kwargs["command_prefix"] = prefix_manager

        if cli_flags.owner and "owner_id" not in kwargs:
            kwargs["owner_id"] = cli_flags.owner

        if "owner_id" not in kwargs:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self._dict_abuse(kwargs))

        if "command_not_found" not in kwargs:
            kwargs["command_not_found"] = "Command {} not found.\n{}"

        self.counter = Counter()
        self.uptime = None
        self.checked_time_accuracy = None
        self.color = discord.Embed.Empty  # This is needed or color ends up 0x000000

        self.main_dir = bot_dir

        self.cog_mgr = CogManager()

        super().__init__(*args, help_command=None, **kwargs)
        # Do not manually use the help formatter attribute here, see `send_help_for`,
        # for a documented API. The internals of this object are still subject to change.
        self._help_formatter = commands.help.RedHelpFormatter()
        self.add_command(commands.help.red_help)

        self._permissions_hooks: List[commands.CheckPredicate] = []

    async def maybe_update_config(self):
        """
        This should be run prior to loading cogs or connecting to discord.
        """
        schema_version = await self.db.schema_version()

        if schema_version == 0:
            await self._schema_0_to_1()
            schema_version += 1
            await self.db.schema_version.set(schema_version)

    async def _schema_0_to_1(self):
        """
        This contains the migration to allow multiple mod and multiple admin roles.
        """

        log.info("Begin updating guild configs to support multiple mod/admin roles")
        all_guild_data = await self.db.all_guilds()
        for guild_id, guild_data in all_guild_data.items():
            guild_obj = discord.Object(id=guild_id)
            mod_roles, admin_roles = [], []
            maybe_mod_role_id = guild_data["mod_role"]
            maybe_admin_role_id = guild_data["admin_role"]

            if maybe_mod_role_id:
                mod_roles.append(maybe_mod_role_id)
                await self.db.guild(guild_obj).mod_role.set(mod_roles)
            if maybe_admin_role_id:
                admin_roles.append(maybe_admin_role_id)
                await self.db.guild(guild_obj).admin_role.set(admin_roles)
        log.info("Done updating guild configs to support multiple mod/admin roles")

    async def send_help_for(
        self, ctx: commands.Context, help_for: Union[commands.Command, commands.GroupMixin, str]
    ):
        """
        Invokes Red's helpformatter for a given context and object.
        """
        return await self._help_formatter.send_help(ctx, help_for)

    async def _dict_abuse(self, indict):
        """
        Please blame <@269933075037814786> for this.

        :param indict:
        :return:
        """

        indict["owner_id"] = await self.db.owner()
        i18n.set_locale(await self.db.locale())

    async def embed_requested(self, channel, user, command=None) -> bool:
        """
        Determine if an embed is requested for a response.

        Parameters
        ----------
        channel : `discord.abc.GuildChannel` or `discord.abc.PrivateChannel`
            The channel to check embed settings for.
        user : `discord.abc.User`
            The user to check embed settings for.
        command
            (Optional) the command ran.

        Returns
        -------
        bool
            :code:`True` if an embed is requested
        """
        if isinstance(channel, discord.abc.PrivateChannel) or (
            command and command == self.get_command("help")
        ):
            user_setting = await self.db.user(user).embeds()
            if user_setting is not None:
                return user_setting
        else:
            guild_setting = await self.db.guild(channel.guild).embeds()
            if guild_setting is not None:
                return guild_setting
        global_setting = await self.db.embeds()
        return global_setting

    async def is_owner(self, user):
        if user.id in self._co_owners:
            return True
        return await super().is_owner(user)

    async def is_admin(self, member: discord.Member):
        """Checks if a member is an admin of their guild."""
        try:
            member_snowflakes = member._roles  # DEP-WARN
            for snowflake in await self.db.guild(member.guild).admin_role():
                if member_snowflakes.has(snowflake):  # Dep-WARN
                    return True
        except AttributeError:  # someone passed a webhook to this
            pass
        return False

    async def is_mod(self, member: discord.Member):
        """Checks if a member is a mod or admin of their guild."""
        try:
            member_snowflakes = member._roles  # DEP-WARN
            for snowflake in await self.db.guild(member.guild).admin_role():
                if member_snowflakes.has(snowflake):  # DEP-WARN
                    return True
            for snowflake in await self.db.guild(member.guild).mod_role():
                if member_snowflakes.has(snowflake):  # DEP-WARN
                    return True
        except AttributeError:  # someone passed a webhook to this
            pass
        return False

    async def get_context(self, message, *, cls=commands.Context):
        return await super().get_context(message, cls=cls)

    async def process_commands(self, message: discord.Message):
        """
        Same as base method, but dispatches an additional event for cogs
        which want to handle normal messages differently to command
        messages,  without the overhead of additional get_context calls
        per cog.
        """
        if not message.author.bot:
            ctx = await self.get_context(message)
            await self.invoke(ctx)
        else:
            ctx = None

        if ctx is None or ctx.valid is False:
            self.dispatch("message_without_command", message)

    @staticmethod
    def list_packages():
        """Lists packages present in the cogs the folder"""
        return os.listdir("cogs")

    async def save_packages_status(self, packages):
        await self.db.packages.set(packages)

    async def add_loaded_package(self, pkg_name: str):
        async with self.db.packages() as curr_pkgs:
            if pkg_name not in curr_pkgs:
                curr_pkgs.append(pkg_name)

    async def remove_loaded_package(self, pkg_name: str):
        async with self.db.packages() as curr_pkgs:
            while pkg_name in curr_pkgs:
                curr_pkgs.remove(pkg_name)

    async def load_extension(self, spec: ModuleSpec):
        # NB: this completely bypasses `discord.ext.commands.Bot._load_from_module_spec`
        name = spec.name.split(".")[-1]
        if name in self.extensions:
            raise errors.PackageAlreadyLoaded(spec)

        lib = spec.loader.load_module()
        if not hasattr(lib, "setup"):
            del lib
            raise discord.ClientException(f"extension {name} does not have a setup function")

        try:
            if asyncio.iscoroutinefunction(lib.setup):
                await lib.setup(self)
            else:
                lib.setup(self)
        except Exception as e:
            self._remove_module_references(lib.__name__)
            self._call_module_finalizers(lib, name)
            raise
        else:
            self._BotBase__extensions[name] = lib

    def remove_cog(self, cogname: str):
        cog = self.get_cog(cogname)
        if cog is None:
            return

        for cls in inspect.getmro(cog.__class__):
            try:
                hook = getattr(cog, f"_{cls.__name__}__permissions_hook")
            except AttributeError:
                pass
            else:
                self.remove_permissions_hook(hook)

        super().remove_cog(cogname)

        for meth in self.rpc_handlers.pop(cogname.upper(), ()):
            self.unregister_rpc_handler(meth)

    async def is_automod_immune(
        self, to_check: Union[discord.Message, commands.Context, discord.abc.User, discord.Role]
    ) -> bool:
        """
        Checks if the user, message, context, or role should be considered immune from automated
        moderation actions.

        This will return ``False`` in direct messages.

        Parameters
        ----------
        to_check : `discord.Message` or `commands.Context` or `discord.abc.User` or `discord.Role`
            Something to check if it would be immune

        Returns
        -------
        bool
            ``True`` if immune

        """
        guild = to_check.guild
        if not guild:
            return False

        if isinstance(to_check, discord.Role):
            ids_to_check = [to_check.id]
        else:
            author = getattr(to_check, "author", to_check)
            try:
                ids_to_check = [r.id for r in author.roles]
            except AttributeError:
                # webhook messages are a user not member,
                # cheaper than isinstance
                return True  # webhooks require significant permissions to enable.
            else:
                ids_to_check.append(author.id)

        immune_ids = await self.db.guild(guild).autoimmune_ids()

        return any(i in immune_ids for i in ids_to_check)

    @staticmethod
    async def send_filtered(
        destination: discord.abc.Messageable,
        filter_mass_mentions=True,
        filter_invite_links=True,
        filter_all_links=False,
        **kwargs,
    ):
        """
        This is a convienience wrapper around

        discord.abc.Messageable.send

        It takes the destination you'd like to send to, which filters to apply
        (defaults on mass mentions, and invite links) and any other parameters
        normally accepted by destination.send

        This should realistically only be used for responding using user provided
        input. (unfortunately, including usernames)
        Manually crafted messages which dont take any user input have no need of this
        """

        content = kwargs.pop("content", None)

        if content:
            if filter_mass_mentions:
                content = common_filters.filter_mass_mentions(content)
            if filter_invite_links:
                content = common_filters.filter_invites(content)
            if filter_all_links:
                content = common_filters.filter_urls(content)

        await destination.send(content=content, **kwargs)

    def add_cog(self, cog: commands.Cog):
        if not isinstance(cog, commands.Cog):
            raise RuntimeError(
                f"The {cog.__class__.__name__} cog in the {cog.__module__} package does "
                f"not inherit from the commands.Cog base class. The cog author must update "
                f"the cog to adhere to this requirement."
            )
        if not hasattr(cog, "requires"):
            commands.Cog.__init__(cog)

        for cls in inspect.getmro(cog.__class__):
            try:
                hook = getattr(cog, f"_{cls.__name__}__permissions_hook")
            except AttributeError:
                pass
            else:
                self.add_permissions_hook(hook)

        for command in cog.__cog_commands__:

            if not isinstance(command, commands.Command):
                raise RuntimeError(
                    f"The {cog.__class__.__name__} cog in the {cog.__module__} package,"
                    " is not using Red's command module, and cannot be added. "
                    "If this is your cog, please use `from redbot.core import commands`"
                    "in place of `from discord.ext import commands`. For more details on "
                    "this requirement, see this page: "
                    "http://red-discordbot.readthedocs.io/en/v3-develop/framework_commands.html"
                )
        super().add_cog(cog)
        self.dispatch("cog_add", cog)
        for command in cog.__cog_commands__:
            self.dispatch("command_add", command)

    def clear_permission_rules(self, guild_id: Optional[int]) -> None:
        """Clear all permission overrides in a scope.

        Parameters
        ----------
        guild_id : Optional[int]
            The guild ID to wipe permission overrides for. If
            ``None``, this will clear all global rules and leave all
            guild rules untouched.

        """
        for cog in self.cogs.values():
            cog.requires.clear_all_rules(guild_id)
        for command in self.walk_commands():
            command.requires.clear_all_rules(guild_id)

    def add_permissions_hook(self, hook: commands.CheckPredicate) -> None:
        """Add a permissions hook.

        Permissions hooks are check predicates which are called before
        calling `Requires.verify`, and they can optionally return an
        override: ``True`` to allow, ``False`` to deny, and ``None`` to
        default to normal behaviour.

        Parameters
        ----------
        hook
            A command check predicate which returns ``True``, ``False``
            or ``None``.

        """
        self._permissions_hooks.append(hook)

    def remove_permissions_hook(self, hook: commands.CheckPredicate) -> None:
        """Remove a permissions hook.

        Parameters are the same as those in `add_permissions_hook`.

        Raises
        ------
        ValueError
            If the permissions hook has not been added.

        """
        self._permissions_hooks.remove(hook)

    async def verify_permissions_hooks(self, ctx: commands.Context) -> Optional[bool]:
        """Run permissions hooks.

        Parameters
        ----------
        ctx : commands.Context
            The context for the command being invoked.

        Returns
        -------
        Optional[bool]
            ``False`` if any hooks returned ``False``, ``True`` if any
            hooks return ``True`` and none returned ``False``, ``None``
            otherwise.

        """
        hook_results = []
        for hook in self._permissions_hooks:
            result = await discord.utils.maybe_coroutine(hook, ctx)
            if result is not None:
                hook_results.append(result)
        if hook_results:
            if all(hook_results):
                ctx.permission_state = commands.PermState.ALLOWED_BY_HOOK
                return True
            else:
                ctx.permission_state = commands.PermState.DENIED_BY_HOOK
                return False

    async def get_owner_notification_destinations(self) -> List[discord.abc.Messageable]:
        """
        Gets the users and channels to send to
        """
        destinations = []
        opt_outs = await self.db.owner_opt_out_list()
        for user_id in (self.owner_id, *self._co_owners):
            if user_id not in opt_outs:
                user = self.get_user(user_id)
                if user:
                    destinations.append(user)

        channel_ids = await self.db.extra_owner_destinations()
        for channel_id in channel_ids:
            channel = self.get_channel(channel_id)
            if channel:
                destinations.append(channel)

        return destinations

    async def send_to_owners(self, content=None, **kwargs):
        """
        This sends something to all owners and their configured extra destinations.

        This takes the same arguments as discord.abc.Messageable.send

        This logs failing sends
        """
        destinations = await self.get_owner_notification_destinations()

        async def wrapped_send(location, content=None, **kwargs):
            try:
                await location.send(content, **kwargs)
            except Exception as _exc:
                log.exception(
                    f"I could not send an owner notification to ({location.id}){location}"
                )

        sends = [wrapped_send(d, content, **kwargs) for d in destinations]
        await asyncio.gather(*sends)


class Red(RedBase, discord.AutoShardedClient):
    """
    You're welcome Caleb.
    """

    async def logout(self):
        """Logs out of Discord and closes all connections."""

        await super().logout()

    async def shutdown(self, *, restart: bool = False):
        """Gracefully quit Red.

        The program will exit with code :code:`0` by default.

        Parameters
        ----------
        restart : bool
            If :code:`True`, the program will exit with code :code:`26`. If the
            launcher sees this, it will attempt to restart the bot.

        """
        if not restart:
            self._shutdown_mode = ExitCodes.SHUTDOWN
        else:
            self._shutdown_mode = ExitCodes.RESTART

        await self.logout()


class ExitCodes(Enum):
    CRITICAL = 1
    SHUTDOWN = 0
    RESTART = 26
