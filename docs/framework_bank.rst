.. V3 Bank

.. role:: python(code)
    :language: python

====
Bank
====

Bank has now been separated from Economy for V3. New to bank is support for
having a global bank.

***********
Basic Usage
***********

.. code-block:: python

    from redbot.core import bank

    class MyCog:
        @commands.command()
        async def balance(self, ctx, user: discord.Member=None):
            if user is None:
                user = ctx.author
            bal = bank.get_balance(user)
            currency = bank.get_currency_name(ctx.guild)
            await ctx.send(
                "{}'s balance is {} {}".format(
                    user.display_name, bal, currency
                )
            )

*************
API Reference
*************

Bank
======

.. automodule:: redbot.core.bank
    :members:
    :exclude-members: cost

    .. autofunction:: cost
        :decorator:
