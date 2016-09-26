#!/usr/bin/env python
from www.db import database, Nominee, Vote
from config import NOMINATIONS
from random import shuffle

database.connect()

# Get a list of nominees
nq = Nominee.select().where(Nominee.chosen)
nominees = [dict() for x in range(len(NOMINATIONS))]
allnoms = {}
for n in nq:
    item = {
            'nom': n.nomination,
            'pos': len(nominees[n.nomination]),
            'who': n.who,
            'votes': 0
           }
    nominees[item['nom']][n.id] = item
    allnoms[n.id] = item

# Now iterate over users' votes and prepare a dict
users = {}
vq = Vote.select().where(~Vote.preliminary)
for v in vq:
    if v.user not in users:
        users[v.user] = [-1] * len(NOMINATIONS)
    n = allnoms[v.nominee.id]
    users[v.user][n['nom']] = n['pos']
    nominees[n['nom']][v.nominee.id]['votes'] += 1

# Print that list nicely
for i, nom in enumerate(NOMINATIONS):
    print(nom)
    for n in sorted(nominees[i].values(), key=lambda x: x['pos']):
        print('- {}: {}'.format(n['who'].encode('utf-8'), n['votes']))
    print('')

# Print the result randomized by user ids
v = users.values()
shuffle(v)
for user in v:
    print(''.join([str(x+1) for x in user]))
