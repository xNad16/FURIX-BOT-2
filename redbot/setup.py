#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import tarfile
from copy import deepcopy
from datetime import datetime as dt
from pathlib import Path
import logging

import appdirs
import click

import redbot.logging
from redbot.core.cli import confirm
from redbot.core.data_manager import (
    basic_config_default,
    load_basic_configuration,
    instance_name,
    basic_config,
    cog_data_path,
    core_data_path,
    storage_details,
)
from redbot.core.utils import safe_delete
from redbot.core import Config
from redbot.core.drivers import BackendType, IdentifierData
from redbot.core.drivers.red_json import JSON

conversion_log = logging.getLogger("red.converter")

config_dir = None
appdir = appdirs.AppDirs("Red-DiscordBot")
if sys.platform == "linux":
    if 0 < os.getuid() < 1000:  # pylint: disable=no-member  # Non-exist on win
        config_dir = Path(appdir.site_data_dir)
if not config_dir:
    config_dir = Path(appdir.user_config_dir)
try:
    config_dir.mkdir(parents=True, exist_ok=True)
except PermissionError:
    print("You don't have permission to write to '{}'\nExiting...".format(config_dir))
    sys.exit(1)
config_file = config_dir / "config.json"


def load_existing_config():
    if not config_file.exists():
        return {}

    with config_file.open(encoding="utf-8") as fs:
        return json.load(fs)


instance_data = load_existing_config()
if instance_data is None:
    instance_list = []
else:
    instance_list = list(instance_data.keys())


def save_config(name, data, remove=False):
    config = load_existing_config()
    if remove and name in config:
        config.pop(name)
    else:
        if name in config:
            print(
                "WARNING: An instance already exists with this name. "
                "Continuing will overwrite the existing instance config."
            )
            if not confirm("Are you absolutely certain you want to continue (y/n)? "):
                print("Not continuing")
                sys.exit(0)
        config[name] = data

    with config_file.open("w", encoding="utf-8") as fs:
        json.dump(config, fs, indent=4)


def get_data_dir():
    default_data_dir = Path(appdir.user_data_dir)

    print(
        "Hello! Before we begin the full configuration process we need to"
        " gather some initial information about where you'd like us"
        " to store your bot's data. We've attempted to figure out a"
        " sane default data location which is printed below. If you don't"
        " want to change this default please press [ENTER], otherwise"
        " input your desired data location."
    )
    print()
    print("Default: {}".format(default_data_dir))

    new_path = input("> ")

    if new_path != "":
        new_path = Path(new_path)
        default_data_dir = new_path

    if not default_data_dir.exists():
        try:
            default_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            print(
                "We were unable to create your chosen directory."
                " You may need to restart this process with admin"
                " privileges."
            )
            sys.exit(1)

    print("You have chosen {} to be your data directory.".format(default_data_dir))
    if not confirm("Please confirm (y/n):"):
        print("Please start the process over.")
        sys.exit(0)
    return default_data_dir


def get_storage_type():
    storage_dict = {1: "JSON", 2: "MongoDB"}
    storage = None
    while storage is None:
        print()
        print("Please choose your storage backend (if you're unsure, choose 1).")
        print("1. JSON (file storage, requires no database).")
        print("2. MongoDB")
        storage = input("> ")
        try:
            storage = int(storage)
        except ValueError:
            storage = None
        else:
            if storage not in storage_dict:
                storage = None
    return storage


def get_name():
    name = ""
    while len(name) == 0:
        print()
        print(
            "Please enter a name for your instance, this name cannot include spaces"
            " and it will be used to run your bot from here on out."
        )
        name = input("> ")
        if " " in name:
            name = ""
    return name


def basic_setup():
    """
    Creates the data storage folder.
    :return:
    """

    default_data_dir = get_data_dir()

    default_dirs = deepcopy(basic_config_default)
    default_dirs["DATA_PATH"] = str(default_data_dir.resolve())

    storage = get_storage_type()

    storage_dict = {1: BackendType.JSON, 2: BackendType.MONGO}
    storage_type: BackendType = storage_dict.get(storage, BackendType.JSON)
    default_dirs["STORAGE_TYPE"] = storage_type.value

    if storage_type == BackendType.MONGO:
        from redbot.core.drivers.red_mongo import get_config_details

        default_dirs["STORAGE_DETAILS"] = get_config_details()
    else:
        default_dirs["STORAGE_DETAILS"] = {}

    name = get_name()
    save_config(name, default_dirs)

    print()
    print(
        "Your basic configuration has been saved. Please run `redbot <name>` to"
        " continue your setup process and to run the bot."
    )


def get_current_backend(instance) -> BackendType:
    return BackendType(instance_data[instance]["STORAGE_TYPE"])


def get_target_backend(backend) -> BackendType:
    if backend == "json":
        return BackendType.JSON
    elif backend == "mongo":
        return BackendType.MONGO


async def json_to_mongov2(instance):
    instance_vals = instance_data[instance]
    current_data_dir = Path(instance_vals["DATA_PATH"])

    load_basic_configuration(instance)

    from redbot.core.drivers import red_mongo

    storage_details = red_mongo.get_config_details()

    core_conf = Config.get_core_conf()
    new_driver = red_mongo.Mongo(cog_name="Core", identifier="0", **storage_details)

    core_conf.init_custom("CUSTOM_GROUPS", 2)
    custom_group_data = await core_conf.custom("CUSTOM_GROUPS").all()

    curr_custom_data = custom_group_data.get("Core", {}).get("0", {})
    exported_data = await core_conf.driver.export_data(curr_custom_data)
    conversion_log.info("Starting Core conversion...")
    await new_driver.import_data(exported_data, curr_custom_data)
    conversion_log.info("Core conversion complete.")

    for p in current_data_dir.glob("cogs/**/settings.json"):
        cog_name = p.parent.stem
        if "." in cog_name:
            # Garbage handler
            continue
        with p.open(mode="r", encoding="utf-8") as f:
            cog_data = json.load(f)
        for identifier, all_data in cog_data.items():
            try:
                conf = Config.get_conf(None, int(identifier), cog_name=cog_name)
            except ValueError:
                continue
            new_driver = red_mongo.Mongo(
                cog_name=cog_name, identifier=conf.driver.unique_cog_identifier, **storage_details
            )

            curr_custom_data = custom_group_data.get(cog_name, {}).get(identifier, {})

            exported_data = await conf.driver.export_data(curr_custom_data)
            conversion_log.info(f"Converting {cog_name} with identifier {identifier}...")
            await new_driver.import_data(exported_data, curr_custom_data)

    conversion_log.info("Cog conversion complete.")

    return storage_details


async def mongov2_to_json(instance):
    load_basic_configuration(instance)

    core_path = core_data_path()

    from redbot.core.drivers import red_json

    core_conf = Config.get_core_conf()
    new_driver = red_json.JSON(cog_name="Core", identifier="0", data_path_override=core_path)

    core_conf.init_custom("CUSTOM_GROUPS", 2)
    custom_group_data = await core_conf.custom("CUSTOM_GROUPS").all()

    curr_custom_data = custom_group_data.get("Core", {}).get("0", {})
    exported_data = await core_conf.driver.export_data(curr_custom_data)
    conversion_log.info("Starting Core conversion...")
    await new_driver.import_data(exported_data, curr_custom_data)
    conversion_log.info("Core conversion complete.")

    collection_names = await core_conf.driver.db.list_collection_names()
    splitted_names = list(
        filter(
            lambda elem: elem[1] != "" and elem[0] != "Core",
            [n.split(".") for n in collection_names],
        )
    )

    ident_map = {}  # Cogname: idents list
    for cog_name, category in splitted_names:
        if cog_name not in ident_map:
            ident_map[cog_name] = set()

        idents = await core_conf.driver.db[cog_name][category].distinct("_id.RED_uuid")
        ident_map[cog_name].update(set(idents))

    for cog_name, idents in ident_map.items():
        for identifier in idents:
            curr_custom_data = custom_group_data.get(cog_name, {}).get(identifier, {})
            try:
                conf = Config.get_conf(None, int(identifier), cog_name=cog_name)
            except ValueError:
                continue
            exported_data = await conf.driver.export_data(curr_custom_data)

            new_path = cog_data_path(raw_name=cog_name)
            new_driver = red_json.JSON(cog_name, identifier, data_path_override=new_path)
            conversion_log.info(f"Converting {cog_name} with identifier {identifier}...")
            await new_driver.import_data(exported_data, curr_custom_data)

    # cog_data_path(raw_name=cog_name)

    conversion_log.info("Cog conversion complete.")

    return {}


async def mongo_to_json(instance):
    load_basic_configuration(instance)

    from redbot.core.drivers.red_mongo import Mongo

    m = Mongo("Core", "0", **storage_details())
    db = m.db
    collection_names = await db.list_collection_names()
    for collection_name in collection_names:
        if "." in collection_name:
            # Fix for one of Zeph's problems
            continue
        elif collection_name == "Core":
            c_data_path = core_data_path()
        else:
            c_data_path = cog_data_path(raw_name=collection_name)
        c_data_path.mkdir(parents=True, exist_ok=True)
        # Every cog name has its own collection
        collection = db[collection_name]
        async for document in collection.find():
            # Every cog has its own document.
            # This means if two cogs have the same name but different identifiers, they will
            # be two separate documents in the same collection
            cog_id = document.pop("_id")
            if not isinstance(cog_id, str):
                # Another garbage data check
                continue
            elif not str(cog_id).isdigit():
                continue
            driver = JSON(collection_name, cog_id, data_path_override=c_data_path)
            for category, value in document.items():
                ident_data = IdentifierData(str(cog_id), category, (), (), {})
                await driver.set(ident_data, value=value)
    return {}


async def edit_instance():
    instance_list = load_existing_config()
    if not instance_list:
        print("No instances have been set up!")
        return

    print(
        "You have chosen to edit an instance. The following "
        "is a list of instances that currently exist:\n"
    )
    for instance in instance_list.keys():
        print("{}\n".format(instance))
    print("Please select one of the above by entering its name")
    selected = input("> ")

    if selected not in instance_list.keys():
        print("That isn't a valid instance!")
        return
    instance_data = instance_list[selected]
    default_dirs = deepcopy(basic_config_default)

    current_data_dir = Path(instance_data["DATA_PATH"])
    print("You have selected '{}' as the instance to modify.".format(selected))
    if not confirm("Please confirm (y/n):"):
        print("Ok, we will not continue then.")
        return

    print("Ok, we will continue on.")
    print()
    if confirm("Would you like to change the instance name? (y/n)"):
        name = get_name()
    else:
        name = selected

    if confirm("Would you like to change the data location? (y/n)"):
        default_data_dir = get_data_dir()
        default_dirs["DATA_PATH"] = str(default_data_dir.resolve())
    else:
        default_dirs["DATA_PATH"] = str(current_data_dir.resolve())

    if name != selected:
        save_config(selected, {}, remove=True)
    save_config(name, default_dirs)

    print("Your basic configuration has been edited")


async def create_backup(instance):
    instance_vals = instance_data[instance]
    if confirm("Would you like to make a backup of the data for this instance? (y/n)"):
        load_basic_configuration(instance)
        if instance_vals["STORAGE_TYPE"] == "MongoDB":
            await mongo_to_json(instance)
        print("Backing up the instance's data...")
        backup_filename = "redv3-{}-{}.tar.gz".format(
            instance, dt.utcnow().strftime("%Y-%m-%d %H-%M-%S")
        )
        pth = Path(instance_vals["DATA_PATH"])
        if pth.exists():
            backup_pth = pth.home()
            backup_file = backup_pth / backup_filename

            to_backup = []
            exclusions = [
                "__pycache__",
                "Lavalink.jar",
                os.path.join("Downloader", "lib"),
                os.path.join("CogManager", "cogs"),
                os.path.join("RepoManager", "repos"),
            ]
            from redbot.cogs.downloader.repo_manager import RepoManager

            repo_mgr = RepoManager()
            await repo_mgr.initialize()
            repo_output = []
            for repo in repo_mgr._repos.values():
                repo_output.append({"url": repo.url, "name": repo.name, "branch": repo.branch})
            repo_filename = pth / "cogs" / "RepoManager" / "repos.json"
            with open(str(repo_filename), "w") as f:
                f.write(json.dumps(repo_output, indent=4))
            instance_vals = {instance_name: basic_config}
            instance_file = pth / "instance.json"
            with open(str(instance_file), "w") as instance_out:
                instance_out.write(json.dumps(instance_vals, indent=4))
            for f in pth.glob("**/*"):
                if not any(ex in str(f) for ex in exclusions):
                    to_backup.append(f)
            with tarfile.open(str(backup_file), "w:gz") as tar:
                for f in to_backup:
                    tar.add(str(f), recursive=False)
            print("A backup of {} has been made. It is at {}".format(instance, backup_file))


async def remove_instance(instance):
    await create_backup(instance)

    instance_vals = instance_data[instance]
    if instance_vals["STORAGE_TYPE"] == "MongoDB":
        from redbot.core.drivers.red_mongo import Mongo

        m = Mongo("Core", **instance_vals["STORAGE_DETAILS"])
        db = m.db
        collections = await db.collection_names(include_system_collections=False)
        for name in collections:
            collection = await db.get_collection(name)
            await collection.drop()
    else:
        pth = Path(instance_vals["DATA_PATH"])
        safe_delete(pth)
    save_config(instance, {}, remove=True)
    print("The instance {} has been removed\n".format(instance))


async def remove_instance_interaction():
    if not instance_list:
        print("No instances have been set up!")
        return

    print(
        "You have chosen to remove an instance. The following "
        "is a list of instances that currently exist:\n"
    )
    for instance in instance_data.keys():
        print("{}\n".format(instance))
    print("Please select one of the above by entering its name")
    selected = input("> ")

    if selected not in instance_data.keys():
        print("That isn't a valid instance!")
        return

    await create_backup(selected)
    await remove_instance(selected)


@click.group(invoke_without_command=True)
@click.option("--debug", type=bool)
@click.pass_context
def cli(ctx, debug):
    level = logging.DEBUG if debug else logging.INFO
    redbot.logging.init_logging(level=level, location=Path.cwd() / "red_setup_logs")
    if ctx.invoked_subcommand is None:
        basic_setup()


@cli.command()
@click.argument("instance", type=click.Choice(instance_list))
def delete(instance):
    loop = asyncio.get_event_loop()
    loop.run_until_complete(remove_instance(instance))


@cli.command()
@click.argument("instance", type=click.Choice(instance_list))
@click.argument("backend", type=click.Choice(["json", "mongo"]))
def convert(instance, backend):
    current_backend = get_current_backend(instance)
    target = get_target_backend(backend)

    default_dirs = deepcopy(basic_config_default)
    default_dirs["DATA_PATH"] = str(Path(instance_data[instance]["DATA_PATH"]))

    loop = asyncio.get_event_loop()

    new_storage_details = None

    if current_backend == BackendType.MONGOV1:
        if target == BackendType.MONGO:
            raise RuntimeError(
                "Please see conversion docs for updating to the latest mongo version."
            )
        elif target == BackendType.JSON:
            new_storage_details = loop.run_until_complete(mongo_to_json(instance))
    elif current_backend == BackendType.JSON:
        if target == BackendType.MONGO:
            new_storage_details = loop.run_until_complete(json_to_mongov2(instance))
    elif current_backend == BackendType.MONGO:
        if target == BackendType.JSON:
            new_storage_details = loop.run_until_complete(mongov2_to_json(instance))

    if new_storage_details is not None:
        default_dirs["STORAGE_TYPE"] = target.value
        default_dirs["STORAGE_DETAILS"] = new_storage_details
        save_config(instance, default_dirs)
        conversion_log.info(f"Conversion to {target} complete.")
    else:
        conversion_log.info(f"Cannot convert {current_backend} to {target} at this time.")


if __name__ == "__main__":
    try:
        cli()  # pylint: disable=no-value-for-parameter  # click
    except KeyboardInterrupt:
        print("Exiting...")
    else:
        print("Exiting...")
