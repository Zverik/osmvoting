#!/usr/bin/env python
from www.db import database, Nominee, Vote
from config import NOMINATIONS
from random import shuffle

database.connect()

# Get a list of nominees
nq = Nominee.select().where(Nominee.status == Nominee.Status.CHOSEN)
nominees = {nom: {} for nom in NOMINATIONS}
allnoms = {}
for n in nq:
    item = {
            'nom': n.category,
            'pos': len(nominees[n.category]),
            'who': n.who,
            'votes': 0
           }
    nominees[n.category][n.id] = item
    allnoms[n.id] = item

# Now iterate over users' votes and prepare a dict
users = {}
vq = Vote.select().where(~Vote.preliminary)
for v in vq:
    if v.user not in users:
        users[v.user] = {nom: [] for nom in NOMINATIONS}
    n = allnoms[v.nominee.id]
    users[v.user][n['nom']].append(n['pos'])
    nominees[n['nom']][v.nominee.id]['votes'] += 1

# Print that list nicely
for nom in NOMINATIONS:
    print(nom)
    for n in sorted(nominees[nom].values(), key=lambda x: x['pos']):
        print('- {}: {}'.format(n['who'].encode('utf-8'), n['votes']))
    print('')

# Print the result randomized by user ids
v = users.values()
shuffle(v)
for user in v:
    print(','.join([''.join([str(x+1) for x in sorted(user[nom])]) for nom in NOMINATIONS]))
