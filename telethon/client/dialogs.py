import itertools
from collections import UserList

from .users import UserMethods
from .. import utils
from ..tl import types, functions, custom


class DialogMethods(UserMethods):

    # region Public methods

    def iter_dialogs(
            self, limit=None, offset_date=None, offset_id=0,
            offset_peer=types.InputPeerEmpty(), _total=None):
        """
        Returns an iterator over the dialogs, yielding 'limit' at most.
        Dialogs are the open "chats" or conversations with other people,
        groups you have joined, or channels you are subscribed to.

        Args:
            limit (`int` | `None`):
                How many dialogs to be retrieved as maximum. Can be set to
                ``None`` to retrieve all dialogs. Note that this may take
                whole minutes if you have hundreds of dialogs, as Telegram
                will tell the library to slow down through a
                ``FloodWaitError``.

            offset_date (`datetime`, optional):
                The offset date to be used.

            offset_id (`int`, optional):
                The message ID to be used as an offset.

            offset_peer (:tl:`InputPeer`, optional):
                The peer to be used as an offset.

            _total (`list`, optional):
                A single-item list to pass the total parameter by reference.

        Yields:
            Instances of `telethon.tl.custom.dialog.Dialog`.
        """
        limit = float('inf') if limit is None else int(limit)
        if limit == 0:
            if not _total:
                return
            # Special case, get a single dialog and determine count
            dialogs = self(functions.messages.GetDialogsRequest(
                offset_date=offset_date,
                offset_id=offset_id,
                offset_peer=offset_peer,
                limit=1
            ))
            _total[0] = getattr(dialogs, 'count', len(dialogs.dialogs))
            return

        seen = set()
        req = functions.messages.GetDialogsRequest(
            offset_date=offset_date,
            offset_id=offset_id,
            offset_peer=offset_peer,
            limit=0
        )
        while len(seen) < limit:
            req.limit = min(limit - len(seen), 100)
            r = self(req)

            if _total:
                _total[0] = getattr(r, 'count', len(r.dialogs))
            entities = {utils.get_peer_id(x): x
                        for x in itertools.chain(r.users, r.chats)}
            messages = {m.id: custom.Message(self, m, entities, None)
                        for m in r.messages}

            # Happens when there are pinned dialogs
            if len(r.dialogs) > limit:
                r.dialogs = r.dialogs[:limit]

            for d in r.dialogs:
                peer_id = utils.get_peer_id(d.peer)
                if peer_id not in seen:
                    seen.add(peer_id)
                    yield (custom.Dialog(self, d, entities, messages))

            if len(r.dialogs) < req.limit\
                    or not isinstance(r, types.messages.DialogsSlice):
                # Less than we requested means we reached the end, or
                # we didn't get a DialogsSlice which means we got all.
                break

            req.offset_date = r.messages[-1].date
            req.offset_peer = entities[utils.get_peer_id(r.dialogs[-1].peer)]
            req.offset_id = r.messages[-1].id
            req.exclude_pinned = True

    def get_dialogs(self, *args, **kwargs):
        """
        Same as :meth:`iter_dialogs`, but returns a list instead
        with an additional ``.total`` attribute on the list.
        """
        total = [0]
        kwargs['_total'] = total
        dialogs = UserList()
        for x in self.iter_dialogs(*args, **kwargs):
            dialogs.append(x)
        dialogs.total = total[0]
        return dialogs

    def iter_drafts(self):  # TODO: Ability to provide a `filter`
        """
        Iterator over all open draft messages.

        Instances of `telethon.tl.custom.draft.Draft` are yielded.
        You can call `telethon.tl.custom.draft.Draft.set_message`
        to change the message or `telethon.tl.custom.draft.Draft.delete`
        among other things.
        """
        r = self(functions.messages.GetAllDraftsRequest())
        for update in r.updates:
            yield (custom.Draft._from_update(self, update))

    def get_drafts(self):
        """
        Same as :meth:`iter_drafts`, but returns a list instead.
        """
        result = []
        for x in self.iter_drafts():
            result.append(x)
        return result

    # endregion
