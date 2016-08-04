from peewee import *
from playhouse.db_url import connect
import config

database = connect(config.DATABASE_URI)


class BaseModel(Model):
    class Meta:
        database = database


class Nominee(BaseModel):
    nomination = IntegerField(index=True)
    who = CharField(max_length=250)
    project = TextField(null=True)
    url = CharField(max_length=1000)
    proposedby = IntegerField(index=True)
    chosen = BooleanField(default=False)


class Vote(BaseModel):
    user = IntegerField(index=True)
    nomination = IntegerField(index=True)
    nominee = ForeignKeyField(Nominee)


def create_tables():
    database.create_tables([Nominee, Vote], safe=True)
