====
Bots
====


.. note::

    These examples assume you have read :ref:`accessing-the-full-api`.


Talking to Inline Bots
**********************

You can query an inline bot, such as `@VoteBot`__ (note, *query*,
not *interact* with a voting message), by making use of the
:tl:`GetInlineBotResultsRequest` request:

.. code-block:: python

    from telethon.tl.functions.messages import GetInlineBotResultsRequest

    bot_results = loop.run_until_complete(client(GetInlineBotResultsRequest(
        bot, user_or_chat, 'query', ''
    )))

And you can select any of their results by using
:tl:`SendInlineBotResultRequest`:

.. code-block:: python

    from telethon.tl.functions.messages import SendInlineBotResultRequest

    loop.run_until_complete(client(SendInlineBotResultRequest(
        get_input_peer(user_or_chat),
        obtained_query_id,
        obtained_str_id
    )))


Talking to Bots with special reply markup
*****************************************

Generally, you just use the `message.click()
<telethon.tl.custom.message.Message.click>` method:

.. code-block:: python

    async def main():
        messages = await client.get_messages('somebot')
        await messages[0].click(0)

You can also do it manually.

To interact with a message that has a special reply markup, such as
`@VoteBot`__ polls, you would use :tl:`GetBotCallbackAnswerRequest`:

.. code-block:: python

    from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

    loop.run_until_complete(client(GetBotCallbackAnswerRequest(
        user_or_chat,
        msg.id,
        data=msg.reply_markup.rows[wanted_row].buttons[wanted_button].data
    )))

It's a bit verbose, but it has all the information you would need to
show it visually (button rows, and buttons within each row, each with
its own data).

__ https://t.me/vote
__ https://t.me/vote
