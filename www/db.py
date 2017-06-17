from peewee import Model, CharField, IntegerField, ForeignKeyField, TextField, BooleanField
from playhouse.db_url import connect
import config

database = connect(config.DATABASE_URI)


class BaseModel(Model):
    class Meta:
        database = database


class Nominee(BaseModel):
    who = CharField(max_length=250)
    project = TextField()
    url = CharField(max_length=1000)
    proposedby = IntegerField(index=True)
    category = CharField(max_length=20, index=True)
    status = IntegerField(choices=(
        (0, 'submitted'),
        (1, 'accepted'),
        (2, 'chosen'),
        (-1, 'duplicate'),
        (-2, 'outoftime'),
        (-3, 'vague'),
        (-4, 'committee'),
        (-5, 'deleted'),
        (-6, 'other'),
    ), index=True)

    class Status:
        SUBMITTED = 0
        ACCEPTED = 1
        CHOSEN = 2
        DUPLICATE = -1
        OUTOFTIME = -2
        VAGUE = -3
        COMMITTEE = -4
        DELETED = -5
        OTHER = -6


class Vote(BaseModel):
    user = IntegerField(index=True)
    nominee = ForeignKeyField(Nominee, related_name='votes')
    preliminary = BooleanField(index=True)


def create_tables():
    database.create_tables([Nominee, Vote], safe=True)
