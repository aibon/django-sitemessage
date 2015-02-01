from collections import OrderedDict

from django import VERSION
from django.utils import six
from django.conf.urls import patterns, url

from .models import Message, Dispatch, Subscription
from .utils import is_iterable, get_registered_messenger_object, get_registered_message_type, \
    import_project_sitemessage_modules, get_registered_messenger_objects, get_registered_message_types, \
    get_message_type_for_app, override_message_type_for_app, register_messenger_objects, \
    register_message_types  # Exposed as toolbox API.
from .exceptions import UnknownMessengerError
from .messages import PlainTextMessage, register_builtin_message_types


_ALIAS_SEP = '|'


if VERSION < (1, 7, 0):
    # Trying import sitemessage settings files from project apps.
    import_project_sitemessage_modules()


def recipients(messenger, addresses):
    """Structures recipients data.

    :param str, MessageBase messenger: MessengerBase heir
    :param list addresses: recipients addresses or Django User model heir instances (NOTE: if supported by a messenger)
    :return: list of Recipient
    :rtype: list
    """
    if isinstance(messenger, six.string_types):
        messenger = get_registered_messenger_object(messenger)
    return messenger._structure_recipients_data(addresses)


def schedule_messages(messages, recipients=None, sender=None, priority=None):
    """Sends a message(s).

    :param MessageBase, str, list messages: str or MessageBase heir or list - use str to create PlainTextMessage.
    :param list recipients: recipients addresses or Django User model heir instances
    :param User sender: User model heir instance
    :param int priority: number describing message priority. If set overrides priority provided with message type.
    :return: list of tuples - (message_model, dispatches_models)
    :rtype: list
    """
    if not is_iterable(messages):
        messages = (messages,)

    results = []
    for message in messages:
        if isinstance(message, six.string_types):
            message = PlainTextMessage(message)

        resulting_priority = message.priority
        if priority is not None:
            resulting_priority = priority
        results.append(message.schedule(sender=sender, recipients=recipients, priority=resulting_priority))

    return results


def send_scheduled_messages(priority=None, ignore_unknown_messengers=False, ignore_unknown_message_types=False):
    """Sends scheduled messages.

    :param int, None priority: number to limit sending message by this priority.
    :param bool ignore_unknown_messengers: to silence UnknownMessengerError
    :param bool ignore_unknown_message_types: to silence UnknownMessageTypeError
    :raises UnknownMessengerError:
    :raises UnknownMessageTypeError:
    """
    dispatches_by_messengers = Dispatch.group_by_messengers(Dispatch.get_unsent(priority=priority))

    for messenger_id, messages in dispatches_by_messengers.items():
        try:
            messenger_obj = get_registered_messenger_object(messenger_id)
            messenger_obj._process_messages(messages, ignore_unknown_message_types=ignore_unknown_message_types)
        except UnknownMessengerError:
            if ignore_unknown_messengers:
                continue
            raise


def prepare_dispatches():
    """Automatically creates dispatches for messages without them.

    :return: list of Dispatch
    :rtype: list
    """
    dispatches = []
    target_messages = Message.get_without_dispatches()

    cache = {}

    for message_model in target_messages:

        if message_model.cls not in cache:
            message_cls = get_registered_message_type(message_model.cls)
            subscribers = message_cls.get_subscribers()
            cache[message_model.cls] = (message_cls, subscribers)
        else:
            message_cls, subscribers = cache[message_model.cls]

        dispatches.extend(message_cls.prepare_dispatches(message_model))

    return dispatches


def get_user_preferences_for_ui(user, message_filter=None, messenger_filter=None, new_messengers_titles=None):
    """Returns a two element tuple with user subscription preferences to render in UI.

    First element:
        A list of messengers titles.

    Second element:
        User preferences dictionary indexed by message type titles.
        Preferences (dictionary values) are lists of tuples:
            (preference_alias, is_supported_by_messenger_flag, user_subscribed_flag)

        Example:
            {'My message type': [('test_message|smtp', True, False), ...]}

    :param User user:
    :param callable|None message_filter: A callable accepting a message object to filter out message types
    :param callable|None messenger_filter: A callable accepting a messenger object to filter out messengers
    :return:
    """
    if new_messengers_titles is None:
        new_messengers_titles = {}

    messengers = get_registered_messenger_objects()
    message_types = get_registered_message_types()
    current_prefs = Subscription.get_for_user(user)

    current_prefs = [
        '%s%s%s' % (pref.message_cls, _ALIAS_SEP, pref.messenger_cls) for pref in current_prefs
    ]

    prefs_by_message_type = {}
    messenger_titles = []

    for messenger in messengers.values():

        if not (messenger_filter is None or messenger_filter(messenger)):
            continue

        title = messenger.title
        new_title = new_messengers_titles.get(messenger.alias)
        messenger_titles.append(new_title or title)

        for message_type in message_types.values():

            if not (message_filter is None or message_filter(message_type)):
                continue

            title = '%s' % message_type.title
            alias = '%s%s%s' % (message_type.alias, _ALIAS_SEP, messenger.alias)

            supported_messengers = message_type.supported_messengers
            is_supported = (not supported_messengers or messenger.alias in supported_messengers)
            is_set = alias in current_prefs

            if title not in prefs_by_message_type:
                prefs_by_message_type[title] = []

            prefs_by_message_type[title].append((alias, is_supported, is_set))

    prefs_by_message_type = OrderedDict(sorted(prefs_by_message_type.items()))

    # Handle messages with the same title: merge into one row.
    messenger_titles_len = len(messenger_titles)
    for title, prefs in prefs_by_message_type.items():
        if len(prefs) > messenger_titles_len:
            prefs_by_message_type[title] = filter(lambda p: p[1] is True, prefs)

    return messenger_titles, prefs_by_message_type


def set_user_preferences_from_request(request):
    """Sets user subscription preferences using data from a request.

    Expects data sent by form built with `sitemessage_prefs_table` template tag.

    :param request:
    :return:
    """
    key = 'sm_user_pref'

    prefs = []
    for pref in request.POST.getlist(key):
        prefs.append(pref.split(_ALIAS_SEP))

    return Subscription.replace_for_user(request.user, prefs)


def get_sitemessage_urls():
    """Returns sitemessage urlpatterns, that can be attached to urlpatterns of a project:

        # Example from urls.py.

        from sitemessage.toolbox import get_sitemessage_urls

        urlpatterns = patterns('',
            # Your URL Patterns belongs here.

        ) + get_sitemessage_urls()  # Now attaching additional URLs.

    """
    return patterns(
        '',
        url(
            r'^messages/unsubscribe/(?P<message_id>\d+)/(?P<dispatch_id>\d+)/(?P<hashed>[^/]+)/$',
            'sitemessage.views.unsubscribe',
            name='sitemessage_unsubscribe'
        )
    )
