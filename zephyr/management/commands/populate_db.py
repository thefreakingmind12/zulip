from django.core.management.base import BaseCommand
from django.utils.timezone import utc

from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from zephyr.models import Message, UserProfile, Stream, Recipient, Client, \
    Subscription, Huddle, get_huddle, Realm, UserMessage, get_user_profile_by_id, \
    bulk_create_realms, bulk_create_streams, bulk_create_users, bulk_create_huddles, \
    bulk_create_clients, \
    do_send_message, clear_database, \
    get_huddle_hash, get_client, do_activate_user
from zephyr.lib.parallel import run_parallel
from django.db import transaction
from django.conf import settings
from api.bots import mit_subs_list
from zephyr.lib.bulk_create import batch_bulk_create

import simplejson
import datetime
import random
import sys
import os
from optparse import make_option

settings.HAVE_TORNADO_SERVER = False

def create_users(realms, name_list):
    user_set = set()
    for full_name, email in name_list:
        (short_name, domain) = email.split("@")
        user_set.add((email, full_name, short_name, True))
    bulk_create_users(realms, user_set)

def create_streams(realms, realm, stream_list):
    stream_set = set()
    for stream_name in stream_list:
        stream_set.add((realm.domain, stream_name))
    bulk_create_streams(realms, stream_set)

class Command(BaseCommand):
    help = "Populate a test database"

    option_list = BaseCommand.option_list + (
        make_option('-n', '--num-messages',
                    dest='num_messages',
                    type='int',
                    default=600,
                    help='The number of messages to create.'),
        make_option('--extra-users',
                    dest='extra_users',
                    type='int',
                    default=0,
                    help='The number of extra users to create'),
        make_option('--huddles',
                    dest='num_huddles',
                    type='int',
                    default=3,
                    help='The number of huddles to create.'),
        make_option('--personals',
                    dest='num_personals',
                    type='int',
                    default=6,
                    help='The number of personal pairs to create.'),
        make_option('--threads',
                    dest='threads',
                    type='int',
                    default=10,
                    help='The number of threads to use.'),
        make_option('--percent-huddles',
                    dest='percent_huddles',
                    type='float',
                    default=15,
                    help='The percent of messages to be huddles.'),
        make_option('--percent-personals',
                    dest='percent_personals',
                    type='float',
                    default=15,
                    help='The percent of messages to be personals.'),
        make_option('--stickyness',
                    dest='stickyness',
                    type='float',
                    default=20,
                    help='The percent of messages to repeat recent folks.'),
        make_option('--nodelete',
                    action="store_false",
                    default=True,
                    dest='delete',
                    help='Whether to delete all the existing messages.'),
        make_option('--replay-old-messages',
                    action="store_true",
                    default=False,
                    dest='replay_old_messages',
                    help='Whether to replace the log of old messages.'),
        )

    def handle(self, **options):
        if options["percent_huddles"] + options["percent_personals"] > 100:
            self.stderr.write("Error!  More than 100% of messages allocated.\n")
            return

        if options["delete"]:
            # Start by clearing all the data in our database
            clear_database()

            # Create our two default realms
            humbug_realm = Realm.objects.create(domain="humbughq.com")
            mit_realm = Realm.objects.create(domain="mit.edu")
            realms = {}
            for realm in Realm.objects.all():
                realms[realm.domain] = realm

            # Create test Users (UserProfiles are automatically created,
            # as are subscriptions to the ability to receive personals).
            names = [("Othello, the Moor of Venice", "othello@humbughq.com"), ("Iago", "iago@humbughq.com"),
                     ("Prospero from The Tempest", "prospero@humbughq.com"),
                     ("Cordelia Lear", "cordelia@humbughq.com"), ("King Hamlet", "hamlet@humbughq.com")]
            for i in xrange(options["extra_users"]):
                names.append(('Extra User %d' % (i,), 'extrauser%d' % (i,)))
            create_users(realms, names)
            # Create public streams.
            stream_list = ["Verona", "Denmark", "Scotland", "Venice", "Rome"]
            create_streams(realms, humbug_realm, stream_list)
            recipient_streams = [recipient.type_id for recipient in
                                 Recipient.objects.filter(type=Recipient.STREAM)]
            # Create subscriptions to streams
            subscriptions_to_add = []
            profiles = UserProfile.objects.select_related().all()
            for i, profile in enumerate(profiles):
                # Subscribe to some streams.
                for type_id in recipient_streams[:int(len(recipient_streams) *
                                                      float(i)/len(profiles)) + 1]:
                    r = Recipient.objects.get(type=Recipient.STREAM, type_id=type_id)
                    s = Subscription(recipient=r, user_profile=profile)
                    subscriptions_to_add.append(s)
            batch_bulk_create(Subscription, subscriptions_to_add)
        else:
            humbug_realm = Realm.objects.get(domain="humbughq.com")
            recipient_streams = [klass.type_id for klass in
                                 Recipient.objects.filter(type=Recipient.STREAM)]

        # Extract a list of all users
        users = [user.id for user in User.objects.all()]

        # Create several initial huddles
        for i in xrange(options["num_huddles"]):
            get_huddle(random.sample(users, random.randint(3, 4)))

        # Create several initial pairs for personals
        personals_pairs = [random.sample(users, 2)
                           for i in xrange(options["num_personals"])]

        threads = options["threads"]
        jobs = []
        for i in xrange(threads):
            count = options["num_messages"] / threads
            if i < options["num_messages"] % threads:
                count += 1
            jobs.append((count, personals_pairs, options, self.stdout.write))
        for status, job in run_parallel(send_messages, jobs, threads=threads):
            pass

        if options["delete"]:
            # Create internal users
            internal_mit_users = []
            create_users(realms, internal_mit_users)
            create_streams(realms, mit_realm, mit_subs_list.all_subs)

            # Now subscribe everyone to these streams
            subscriptions_to_add = []
            profiles = UserProfile.objects.select_related().filter(realm=mit_realm)
            for cls in mit_subs_list.all_subs:
                stream = Stream.objects.get(name=cls, realm=mit_realm)
                recipient = Recipient.objects.get(type=Recipient.STREAM, type_id=stream.id)
                for profile in profiles:
                    if profile.user.email in mit_subs_list.subs_lists:
                        key = profile.user.email
                    else:
                        key = "default"
                    if cls in mit_subs_list.subs_lists[key]:
                        s = Subscription(recipient=recipient,
                                         user_profile=profile)
                        subscriptions_to_add.append(s)
            batch_bulk_create(Subscription, subscriptions_to_add)

            internal_humbug_users = []
            create_users(realms, internal_humbug_users)
            humbug_stream_list = ["devel", "all", "humbug", "design", "support", "social", "test"]
            create_streams(realms, humbug_realm, humbug_stream_list)

            # Now subscribe everyone to these streams
            subscriptions_to_add = []
            profiles = UserProfile.objects.select_related().filter(realm=humbug_realm)
            for cls in humbug_stream_list:
                stream = Stream.objects.get(name=cls, realm=humbug_realm)
                recipient = Recipient.objects.get(type=Recipient.STREAM, type_id=stream.id)
                for profile in profiles:
                    # Subscribe to some streams.
                    s = Subscription(recipient=recipient, user_profile=profile)
                    subscriptions_to_add.append(s)
            batch_bulk_create(Subscription, subscriptions_to_add)

            self.stdout.write("Successfully populated test database.\n")
        if options["replay_old_messages"]:
            restore_saved_messages()
            site = Site.objects.get_current()
            site.domain = 'humbughq.com'
            site.save()

            # Set restored pointers to the very latest messages
            for user_profile in UserProfile.objects.all():
                ids = [u.message_id for u in UserMessage.objects.filter(user_profile = user_profile)]
                if ids != []:
                    user_profile.pointer = max(ids)
                    user_profile.save()


recipient_hash = {}
def get_recipient_by_id(rid):
    if rid in recipient_hash:
        return recipient_hash[rid]
    return Recipient.objects.get(id=rid)

def restore_saved_messages():
    old_messages = []
    duplicate_suppression_hash = {}

    stream_set = set()
    user_set = set()
    email_set = set(u.email for u in User.objects.all())
    realm_set = set()
    # Initial client_set is nonempty temporarily because we don't have
    # clients in logs at all right now -- later we can start with nothing.
    client_set = set(["populate_db", "website", "zephyr_mirror"])
    huddle_user_set = set()
    # First, determine all the objects our messages will need.
    print datetime.datetime.now(), "Creating realms/streams/etc..."
    for line in file("all_messages_log", "r").readlines():
        old_message_json = line.strip()

        # Due to populate_db's shakespeare mode, we have a lot of
        # duplicate messages in our log that only differ in their
        # logged ID numbers (same timestamp, content, etc.).  With
        # sqlite, bulk creating those messages won't work properly: in
        # particular, the first 100 messages will actually only result
        # in 20 rows ending up in the target table, which screws up
        # the below accounting where for handling changing
        # subscriptions, we assume that the Nth row populate_db
        # created goes with the Nth non-subscription row of the input
        # So suppress the duplicates when using sqlite.
        if "sqlite" in settings.DATABASES["default"]["ENGINE"]:
            tmp_message = simplejson.loads(old_message_json)
            tmp_message['id'] = '1'
            duplicate_suppression_key = simplejson.dumps(tmp_message)
            if duplicate_suppression_key in duplicate_suppression_hash:
                continue
            duplicate_suppression_hash[duplicate_suppression_key] = True

        old_message = simplejson.loads(old_message_json)
        message_type = old_message["type"]

        # Lower case a bunch of fields that will screw up
        # deduplication if we don't
        if message_type.startswith("subscription"):
            old_message["name"] = old_message["name"].lower()
            old_message["domain"] = old_message["domain"].lower()
        elif message_type.startswith("user_"):
            old_message["user"] = old_message["user"].lower()
        else:
            old_message["sender_email"] = old_message["sender_email"].lower()

        if message_type == 'stream':
            old_message["recipient"] = old_message["recipient"].lower()
        elif message_type == 'personal':
            old_message["recipient"][0]["email"] = old_message["recipient"][0]["email"].lower()
        elif message_type == "huddle":
            for i in xrange(len(old_message["recipient"])):
                old_message["recipient"][i]["email"] = old_message["recipient"][i]["email"].lower()

        old_messages.append(old_message)

        if message_type.startswith("subscription"):
            stream_set.add((old_message["domain"], old_message["name"]))
            continue
        elif message_type.startswith("user_"):
            continue
        sender_email = old_message["sender_email"]
        domain = sender_email.split('@')[1]
        realm_set.add(domain)

        if old_message["sender_email"] not in email_set:
            user_set.add((old_message["sender_email"],
                          old_message["sender_full_name"],
                          old_message["sender_short_name"],
                          False))

        if 'sending_client' in old_message:
            client_set.add(old_message['sending_client'])

        if message_type == 'stream':
            stream_set.add((domain, old_message['recipient']))
        elif message_type == 'personal':
            u = old_message["recipient"][0]
            if u["email"] not in email_set:
                user_set.add((u["email"], u["full_name"], u["short_name"], False))
                email_set.add(u["email"])
        elif message_type == 'huddle':
            for u in old_message["recipient"]:
                user_set.add((u["email"], u["full_name"], u["short_name"], False))
                if u["email"] not in email_set:
                    user_set.add((u["email"], u["full_name"], u["short_name"], False))
                    email_set.add(u["email"])
            huddle_user_set.add(tuple(sorted(u["email"] for u in old_message["recipient"])))
        else:
            raise

    stream_recipients = {}
    user_recipients = {}
    huddle_recipients = {}

    # Then, create the objects our messages need.
    print datetime.datetime.now(), "Creating realms..."
    bulk_create_realms(realm_set)

    realms = {}
    for realm in Realm.objects.all():
        realms[realm.domain] = realm

    print datetime.datetime.now(), "Creating clients..."
    bulk_create_clients(client_set)

    clients = {}
    for client in Client.objects.all():
        clients[client.name] = client

    print datetime.datetime.now(), "Creating streams..."
    bulk_create_streams(realms, stream_set)

    streams = {}
    for stream in Stream.objects.all():
        streams[stream.id] = stream
    for recipient in Recipient.objects.filter(type=Recipient.STREAM):
        stream_recipients[(streams[recipient.type_id].realm_id,
                           streams[recipient.type_id].name.lower())] = recipient

    print datetime.datetime.now(), "Creating users..."
    bulk_create_users(realms, user_set)

    users = {}
    users_by_id = {}
    for user_profile in UserProfile.objects.select_related().all():
        users[(user_profile.user.email)] = user_profile
        users_by_id[user_profile.id] = user_profile
    for recipient in Recipient.objects.filter(type=Recipient.PERSONAL):
        user_recipients[users_by_id[recipient.type_id].user.email] = recipient

    print datetime.datetime.now(), "Creating huddles..."
    bulk_create_huddles(users, huddle_user_set)

    huddles_by_id = {}
    for huddle in Huddle.objects.all():
        huddles_by_id[huddle.id] = huddle
    for recipient in Recipient.objects.filter(type=Recipient.HUDDLE):
        huddle_recipients[huddles_by_id[recipient.type_id].huddle_hash] = recipient

    # TODO: Add a special entry type in the log that is a subscription
    # change and import those as we go to make subscription changes
    # take effect!
    print datetime.datetime.now(), "Importing subscriptions..."
    subscribers = {}
    for s in Subscription.objects.select_related().all():
        if s.active:
            subscribers.setdefault(s.recipient.id, set()).add(s.user_profile.id)

    # Then create all the messages, without talking to the DB!
    print datetime.datetime.now(), "Importing messages, part 1..."
    first_message_id = None
    if Message.objects.exists():
        first_message_id = Message.objects.all().order_by("-id")[0].id + 1

    messages_to_create = []
    for idx, old_message in enumerate(old_messages):
        if (old_message["type"].startswith("subscription") or
            old_message["type"].startswith("user_")):
            continue

        message = Message()

        sender_email = old_message["sender_email"]
        domain = sender_email.split('@')[1]
        realm = realms[domain]

        message.sender = users[sender_email]
        type_hash = {"stream": Recipient.STREAM,
                     "huddle": Recipient.HUDDLE,
                     "personal": Recipient.PERSONAL}

        if 'sending_client' in old_message:
            message.sending_client = clients[old_message['sending_client']]
        elif sender_email in ["othello@humbughq.com", "iago@humbughq.com", "prospero@humbughq.com",
                              "cordelia@humbughq.com", "hamlet@humbughq.com"]:
            message.sending_client = clients['populate_db']
        elif realm.domain == "humbughq.com":
            message.sending_client = clients["website"]
        elif realm.domain == "mit.edu":
            message.sending_client = clients['zephyr_mirror']
        else:
            message.sending_client = clients['populate_db']

        message.type = type_hash[old_message["type"]]
        message.content = old_message["content"]
        message.subject = old_message["subject"]
        ts = float(old_message["timestamp"])
        message.pub_date = datetime.datetime.utcfromtimestamp(ts).replace(tzinfo=utc)

        if message.type == Recipient.PERSONAL:
            message.recipient = user_recipients[old_message["recipient"][0]["email"]]
        elif message.type == Recipient.STREAM:
            message.recipient = stream_recipients[(realm.id,
                                                   old_message["recipient"])]
        elif message.type == Recipient.HUDDLE:
            huddle_hash = get_huddle_hash([users[u["email"]].id
                                           for u in old_message["recipient"]])
            message.recipient = huddle_recipients[huddle_hash]
        else:
            raise
        messages_to_create.append(message)

    print datetime.datetime.now(), "Importing messages, part 2..."
    batch_bulk_create(Message, messages_to_create, batch_size=100)

    # Finally, create all the UserMessage objects
    print datetime.datetime.now(), "Importing usermessages, part 1..."
    personal_recipients = {}
    for r in Recipient.objects.filter(type = Recipient.PERSONAL):
        personal_recipients[r.id] = True

    all_messages = Message.objects.all()
    user_messages_to_create = []

    messages_by_id = {}
    for message in all_messages:
        messages_by_id[message.id] = message

    if first_message_id is None:
        first_message_id = min(messages_by_id.keys())

    pending_subs = {}
    current_message_id = first_message_id
    for old_message in old_messages:
        # Update our subscribers hashes as we see subscription events
        if old_message["type"] == 'subscription_added':
            stream_key = (realms[old_message["domain"]].id, old_message["name"])
            subscribers.setdefault(stream_recipients[stream_key].id,
                                   set()).add(users[old_message["user"]].id)
            pending_subs[(stream_recipients[stream_key].id,
                          users[old_message["user"]].id)] = True
            continue
        elif old_message["type"] == "subscription_removed":
            stream_key = (realms[old_message["domain"]].id, old_message["name"])
            subscribers.setdefault(stream_recipients[stream_key].id,
                                   set()).remove(users[old_message["user"]].id)
            pending_subs[(stream_recipients[stream_key].id,
                          users[old_message["user"]].id)] = False
            continue
        elif old_message["type"] == "user_activated":
            # These are rare, so just handle them the slow way
            user = User.objects.get(email=old_message["user"])
            do_activate_user(user, log=False)
            # Update the cache of users to show this user as activated
            users_by_id[user.userprofile.id] = UserProfile.objects.get(user=user)
            continue
        elif old_message["type"] == "user_change_password":
            # Just handle these the slow way
            user = User.objects.get(email=old_message["user"])
            user.password = old_message["pwhash"]
            user.save()
            continue
        elif old_message["type"] == "user_change_full_name":
            # Just handle these the slow way
            user_profile = UserProfile.objects.get(user__email=old_message["user"])
            user_profile.full_name = old_message["full_name"]
            user_profile.save()
            continue

        message = messages_by_id[current_message_id]
        current_message_id += 1

        if message.recipient_id not in subscribers:
            # Nobody received this message -- probably due to our
            # subscriptions being out-of-date.
            continue
        for user_profile_id in subscribers[message.recipient_id]:
            if users_by_id[user_profile_id].user.is_active:
                um = UserMessage(user_profile_id=user_profile_id,
                                 message=message)
                user_messages_to_create.append(um)
        if message.recipient_id in personal_recipients:
            # Include the sender in huddle recipients
            if users_by_id[message.sender_id].user.is_active:
                um = UserMessage(user_profile_id=message.sender_id,
                                 message=message)
                user_messages_to_create.append(um)

    print datetime.datetime.now(), "Importing usermessages, part 2..."
    tot_user_messages = len(user_messages_to_create)
    batch_bulk_create(UserMessage, user_messages_to_create)

    print datetime.datetime.now(), "Finalizing subscriptions..."
    current_subs = {}
    current_subs_obj = {}
    for s in Subscription.objects.select_related().all():
        current_subs[(s.recipient_id, s.user_profile_id)] = s.active
        current_subs_obj[(s.recipient_id, s.user_profile_id)] = s

    subscriptions_to_add = []
    subscriptions_to_change = []
    for pending_sub in pending_subs.keys():
        (recipient_id, user_profile_id) = pending_sub
        current_state = current_subs.get(pending_sub)
        if pending_subs[pending_sub] == current_state:
            # Already correct in the database
            continue
        elif current_state is not None:
            subscriptions_to_change.append((pending_sub, pending_subs[pending_sub]))
            continue

        s = Subscription(recipient_id=recipient_id,
                         user_profile_id=user_profile_id,
                         active=pending_subs[pending_sub])
        subscriptions_to_add.append(s)
    batch_bulk_create(Subscription, subscriptions_to_add)
    with transaction.commit_on_success():
        for (sub, active) in subscriptions_to_change:
            current_subs_obj[sub].active = active
            current_subs_obj[sub].save()

    print datetime.datetime.now(), "Finished importing %s messages (%s usermessages)" % \
        (len(all_messages), tot_user_messages)

    # Clear the remainder of the line, using a terminal control code.
    sys.stderr.write('\rDone replaying old messages.\x1B[0K\n')

# Create some test messages, including:
# - multiple streams
# - multiple subjects per stream
# - multiple huddles
# - multiple personals converastions
# - multiple messages per subject
# - both single and multi-line content
def send_messages(data):
    (tot_messages, personals_pairs, options, output) = data
    random.seed(os.getpid())
    from django.db import connection
    connection.close()
    texts = file("zephyr/management/commands/test_messages.txt", "r").readlines()
    offset = random.randint(0, len(texts))

    recipient_streams = [klass.id for klass in
                         Recipient.objects.filter(type=Recipient.STREAM)]
    recipient_huddles = [h.id for h in Recipient.objects.filter(type=Recipient.HUDDLE)]

    huddle_members = {}
    for h in recipient_huddles:
        huddle_members[h] = [s.user_profile.id for s in
                             Subscription.objects.filter(recipient_id=h)]

    num_messages = 0
    random_max = 1000000
    recipients = {}
    while num_messages < tot_messages:
      with transaction.commit_on_success():
        saved_data = ''
        message = Message()
        message.sending_client = get_client('populate_db')
        length = random.randint(1, 5)
        lines = (t.strip() for t in texts[offset: offset + length])
        message.content = '\n'.join(lines)
        offset += length
        offset = offset % len(texts)

        randkey = random.randint(1, random_max)
        if (num_messages > 0 and
            random.randint(1, random_max) * 100. / random_max < options["stickyness"]):
            # Use an old recipient
            message_type, recipient_id, saved_data = recipients[num_messages - 1]
            if message_type == Recipient.PERSONAL:
                personals_pair = saved_data
                random.shuffle(personals_pair)
            elif message_type == Recipient.STREAM:
                message.subject = saved_data
                message.recipient = get_recipient_by_id(recipient_id)
            elif message_type == Recipient.HUDDLE:
                message.recipient = get_recipient_by_id(recipient_id)
        elif (randkey <= random_max * options["percent_huddles"] / 100.):
            message_type = Recipient.HUDDLE
            message.recipient = get_recipient_by_id(random.choice(recipient_huddles))
        elif (randkey <= random_max * (options["percent_huddles"] + options["percent_personals"]) / 100.):
            message_type = Recipient.PERSONAL
            personals_pair = random.choice(personals_pairs)
            random.shuffle(personals_pair)
        elif (randkey <= random_max * 1.0):
            message_type = Recipient.STREAM
            message.recipient = get_recipient_by_id(random.choice(recipient_streams))

        if message_type == Recipient.HUDDLE:
            sender_id = random.choice(huddle_members[message.recipient.id])
            message.sender = get_user_profile_by_id(sender_id)
        elif message_type == Recipient.PERSONAL:
            message.recipient = Recipient.objects.get(type=Recipient.PERSONAL,
                                                         type_id=personals_pair[0])
            message.sender = get_user_profile_by_id(personals_pair[1])
            saved_data = personals_pair
        elif message_type == Recipient.STREAM:
            stream = Stream.objects.get(id=message.recipient.type_id)
            # Pick a random subscriber to the stream
            message.sender = random.choice(Subscription.objects.filter(
                    recipient=message.recipient)).user_profile
            message.subject = stream.name + str(random.randint(1, 3))
            saved_data = message.subject

        message.pub_date = datetime.datetime.utcnow().replace(tzinfo=utc)
        do_send_message(message)

        recipients[num_messages] = [message_type, message.recipient.id, saved_data]
        num_messages += 1
    return tot_messages
