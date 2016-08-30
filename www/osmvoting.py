from www import app
from db import database, Nominee, Vote
from flask import session, url_for, redirect, request, render_template, g
from flask_oauthlib.client import OAuth
from flask_wtf import Form
from wtforms import StringField
from wtforms.validators import DataRequired, Optional, URL
from playhouse.shortcuts import model_to_dict
from datetime import date
from peewee import JOIN, fn
import yaml
import os
import config
import codecs

oauth = OAuth()
openstreetmap = oauth.remote_app('OpenStreetMap',
                                 base_url='https://api.openstreetmap.org/api/0.6/',
                                 request_token_url='https://www.openstreetmap.org/oauth/request_token',
                                 access_token_url='https://www.openstreetmap.org/oauth/access_token',
                                 authorize_url='https://www.openstreetmap.org/oauth/authorize',
                                 consumer_key=app.config['OAUTH_KEY'] or '123',
                                 consumer_secret=app.config['OAUTH_SECRET'] or '123'
                                 )


@app.before_request
def before_request():
    database.connect()
    load_user_language()


@app.teardown_request
def teardown(exception):
    if not database.is_closed():
        database.close()


def load_user_language():
    supported = set([x[:x.index('.')].decode('utf-8') for x in os.listdir(os.path.join(config.BASE_DIR, 'lang')) if '.yaml' in x])
    accepted = request.headers.get('Accept-Language', '')
    lang = 'en'
    for lpart in accepted.split(','):
        if ';' in lpart:
            lpart = lpart[:lpart.index(';')]
        pieces = lpart.strip().split('-')
        if len(pieces) >= 2:
            testlang = '{}_{}'.format(pieces[0].lower(), pieces[1].upper())
            if testlang in supported:
                lang = testlang
                break
        if len(pieces) == 1 and pieces[0].lower() in supported:
            lang = pieces[0].lower()
            break

    # Load language
    with codecs.open(os.path.join(config.BASE_DIR, 'lang', lang + '.yaml'), 'r', 'utf-8') as f:
        data = yaml.load(f)
        g.lang = data[data.keys()[0]]


@app.route('/')
def login():
    if 'osm_token' not in session:
        session['objects'] = request.args.get('objects')
        return openstreetmap.authorize(callback=url_for('oauth'))
    if config.STAGE in ('call', 'select'):
        return redirect(url_for('edit_nominees'))
    return 'Unknown stage: {0}'.format(config.STAGE)


@app.route('/oauth')
def oauth():
    resp = openstreetmap.authorized_response()
    if resp is None:
        return 'Denied. <a href="' + url_for('login') + '">Try again</a>.'
    session['osm_token'] = (
            resp['oauth_token'],
            resp['oauth_token_secret']
    )
    user_details = openstreetmap.get('user/details').data
    session['osm_uid'] = int(user_details[0].get('id'))
    return redirect(url_for('login'))


@openstreetmap.tokengetter
def get_token(token='user'):
    if token == 'user' and 'osm_token' in session:
        return session['osm_token']
    return None


@app.route('/logout')
def logout():
    if 'osm_token' in session:
        del session['osm_token']
    if 'osm_uid' in session:
        del session['osm_uid']
    return 'Logged out.'


class AddNomineeForm(Form):
    who = StringField('Who', validators=[DataRequired()])
    project = StringField('For what')
    url = StringField('URL', validators=[Optional(), URL()])


@app.route('/nominees')
@app.route('/nominees/<n>')
def edit_nominees(n=None, form=None):
    """Called from login(), a convenience method."""
    if 'osm_token' not in session:
        return redirect(url_for('login'))
    if 'nomination' not in session:
        session['nomination'] = 0
    if n is not None:
        if len(n) == 1 and n.isdigit() and int(n) < len(config.NOMINATIONS):
            session['nomination'] = int(n)
        elif n in config.NOMINATIONS:
            session['nomination'] = config.NOMINATIONS.index(n)
    nom = session['nomination']

    tmp_obj = None
    if 'tmp_nominee' in session:
        tmp_obj = session['tmp_nominee']
        del session['tmp_nominee']
    form = AddNomineeForm(data=tmp_obj)
    uid = session['osm_uid']
    isadmin = uid in config.ADMINS
    nominees = Nominee.select(Nominee, Vote.user.alias('voteuser')).where(Nominee.nomination == nom).join(
        Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.user == uid) & (Vote.preliminary))).naive()
    canadd = isadmin or (config.STAGE == 'call' and Nominee.select().where((Nominee.proposedby == uid) & (Nominee.nomination == nom)).count() < 10)
    if isteam(uid):
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
        # Now for the team votes
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.preliminary) & (Vote.user << list(config.TEAM)))).group_by(Nominee.id)
        teamvotes = {}
        if isadmin:
            for v in votesq:
                teamvotes[v.id] = v.num_votes
    else:
        votes = None
        teamvotes = None
    return render_template('index.html',
                           form=form, nomination=config.NOMINATIONS[nom],
                           nominees=nominees, user=uid, isadmin=isadmin, canvote=canvote(uid),
                           canunvote=config.STAGE == 'call' or isteam(uid),
                           votes=votes, teamvotes=teamvotes,
                           year=date.today().year, stage=config.STAGE, canadd=canadd,
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/add', methods=['POST'])
def add_nominee():
    if 'osm_token' not in session or not canvote(session['osm_uid']):
        return redirect(url_for('login'))
    form = AddNomineeForm()
    if form.validate():
        n = Nominee()
        form.populate_obj(n)
        n.nomination = session['nomination']
        n.proposedby = session['osm_uid']
        n.save()
        return redirect(url_for('edit_nominees'))
    return 'Error in fields:\n{}'.format('\n'.join(['{}: {}'.format(k, v) for k, v in form.errors.items()]))


@app.route('/delete/<nid>')
def delete_nominee(nid):
    if 'osm_token' not in session or config.STAGE != 'call':
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    session['tmp_nominee'] = model_to_dict(n)
    n.delete_instance(recursive=True)
    return redirect(url_for('edit_nominees'))


def canvote(uid):
    if config.STAGE != 'call' and not isteam(uid):
        return False
    return Vote.select().join(Nominee).where(
        (Vote.user == uid) & (Vote.preliminary) & (Nominee.nomination == session['nomination'])).count() < 5


def isteam(uid):
    return config.STAGE == 'select' and uid in config.TEAM


@app.route('/prevote/<nid>')
def prevote(nid):
    if 'osm_token' not in session:
        return redirect(url_for('login'))
    uid = session['osm_uid']
    if config.STAGE != 'call' and not isteam(uid):
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    try:
        v = Vote.get((Vote.user == uid) & (Vote.nominee == n) & (Vote.preliminary))
        v.delete_instance()
    except Vote.DoesNotExist:
        if canvote(uid):
            v = Vote()
            v.nominee = n
            v.user = uid
            v.preliminary = True
            v.save()
    return redirect(url_for('edit_nominees'))
