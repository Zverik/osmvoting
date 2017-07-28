#!/usr/bin/env python
from www.db import database, Nominee
from collections import defaultdict

database.connect()

# Get a list of nominees
nq = Nominee.select().where(Nominee.status == Nominee.Status.CHOSEN)
result = defaultdict(dict)
for n in nq:
    item = {'desc': n.project.replace('\r', '').replace('\n', ' ').replace('  ', ' '), 'who': n.who}
    result[n.category][n.id] = item

print 'en:'
for cat, nominees in result.iteritems():
    print '  {}:'.format(cat)
    for nid, nom in nominees.iteritems():
        print '    # {}'.format(nom['who'])
        print '    {}: >\n      {}'.format(nid, nom['desc'].encode('utf-8'))
