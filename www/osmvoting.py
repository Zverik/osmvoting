from www import app
from db import database, Nominee, Vote
from flask import session, url_for, redirect, request, render_template, g, flash
from flask_oauthlib.client import OAuth
from flask_wtf import Form
from wtforms import StringField, HiddenField
from wtforms.validators import DataRequired, Optional, URL
from playhouse.shortcuts import model_to_dict
from datetime import date
from random import Random
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
    supported = set([x[:x.index('.')].decode('utf-8') for x in os.listdir(
        os.path.join(config.BASE_DIR, 'lang')) if '.yaml' in x])
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
    if config.STAGE == 'processing':
        return redirect(url_for('wait'))
    if 'osm_token' not in session:
        session['objects'] = request.args.get('objects')
        return openstreetmap.authorize(callback=url_for('oauth'))
    if config.STAGE in ('call', 'select'):
        return redirect(url_for('edit_nominees'))
    if config.STAGE == 'voting':
        return redirect(url_for('voting'))
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
    nomid = HiddenField('Nominee IS', validators=[Optional()])


@app.route('/nominees')
@app.route('/nominees/<n>')
def edit_nominees(n=None, form=None):
    """Called from login(), a convenience method."""
    # Temporary redirect to voting
    if config.STAGE not in ('call', 'select'):
        return redirect(url_for('login'))
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
    canadd = isadmin or (config.STAGE == 'call' and Nominee.select().where(
        (Nominee.proposedby == uid) & (Nominee.nomination == nom)).count() < 10)
    if isteam(uid):
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
        # Now for the team votes
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).join(Vote, JOIN.LEFT_OUTER, on=(
                (Vote.nominee == Nominee.id) & (Vote.preliminary) & (Vote.user << list(config.TEAM)))).group_by(Nominee.id)
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
        if form.nomid.data.isdigit():
            n = Nominee.get(Nominee.id == int(form.nomid.data))
        else:
            n = Nominee()
            n.nomination = session['nomination']
            n.proposedby = session['osm_uid']
        form.populate_obj(n)
        n.save()
        return redirect(url_for('edit_nominees'))
    return 'Error in fields:\n{}'.format('\n'.join(['{}: {}'.format(k, v) for k, v in form.errors.items()]))


@app.route('/delete/<nid>')
def delete_nominee(nid):
    if 'osm_token' not in session or (
            config.STAGE != 'call' and session['osm_uid'] not in config.ADMINS):
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    session['tmp_nominee'] = model_to_dict(n)
    n.delete_instance(recursive=True)
    return redirect(url_for('edit_nominees'))


@app.route('/edit/<nid>')
def edit_nominee(nid):
    if 'osm_token' not in session or session['osm_uid'] not in config.ADMINS:
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    n2 = model_to_dict(n)
    n2['nomid'] = nid
    session['tmp_nominee'] = n2
    return redirect(url_for('edit_nominees'))


@app.route('/choose/<nid>')
def choose_nominee(nid):
    if 'osm_token' not in session or session['osm_uid'] not in config.ADMINS:
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    n.chosen = not n.chosen
    print n.chosen
    n.save()
    return redirect(url_for('edit_nominees'))


def canvote(uid):
    if session['osm_uid'] in config.ADMINS:
        return True
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


@app.route('/list')
def list_chosen():
    nominees = Nominee.select().where(Nominee.chosen)
    return render_template('list.html',
                           nominees=nominees, year=date.today().year,
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/voting')
def voting():
    """Called from login(), a convenience method."""
    if 'osm_token' not in session:
        return redirect(url_for('login'))
    if config.STAGE != 'voting':
        return redirect(url_for('login'))

    uid = session['osm_uid']
    isadmin = uid in config.ADMINS
    nominees_list = Nominee.select(Nominee, Vote.user.alias('voteuser')).where(Nominee.chosen).join(
        Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.user == uid) & (~Vote.preliminary))).naive()
    # Shuffle the nominees
    nominees = [n for n in nominees_list]
    rnd = Random()
    rnd.seed(uid)
    rnd.shuffle(nominees)
    # For admin, populate the dict of votes
    if isadmin:
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).where(Nominee.chosen).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (~Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
    else:
        votes = None
    # Count total number of voters
    total = Vote.select(fn.Distinct(Vote.user)).where(~Vote.preliminary).group_by(Vote.user).count()
    # Yay, done
    return render_template('voting.html',
                           nominees=nominees, year=date.today().year,
                           isadmin=isadmin, votes=votes, stage=config.STAGE,
                           total=total,
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/votes', methods=['POST'])
def vote_all():
    if 'osm_token' not in session or config.STAGE != 'voting':
        return redirect(url_for('login'))
    uid = session['osm_uid']
    for nom in range(len(config.NOMINATIONS)):
        vote = request.form.get('vote{}'.format(nom), -1, type=int)
        if vote < 0:
            continue
        try:
            # Delete votes from the same category by this voter
            v = Vote.select().where((Vote.user == uid) & (~Vote.preliminary)).join(Nominee).where(
                Nominee.nomination == nom).get()
            v.delete_instance()
        except Vote.DoesNotExist:
            pass
        if vote > 0:
            v = Vote()
            v.nominee = Nominee.get(Nominee.id == vote)
            v.user = uid
            v.preliminary = False
            v.save()
    flash(g.lang['thanksvoted'])
    return redirect(url_for('voting'))


@app.route('/vote/<nid>')
def vote(nid):
    if 'osm_token' not in session or config.STAGE != 'voting':
        return redirect(url_for('login'))
    uid = session['osm_uid']
    n = Nominee.get(Nominee.id == nid)
    try:
        # Delete votes from the same category by this voter
        v = Vote.select().where((Vote.user == uid) & (~Vote.preliminary)).join(Nominee).where(
            Nominee.nomination == n.nomination).get()
        v.delete_instance()
    except Vote.DoesNotExist:
        pass
    v = Vote()
    v.nominee = n
    v.user = uid
    v.preliminary = False
    v.save()
    return redirect(url_for('voting'))


@app.route('/wait')
def wait():
    uid = session['osm_uid'] if 'osm_uid' in session else 0
    isadmin = uid in config.ADMINS
    nominees = Nominee.select().where(Nominee.chosen)
    # For admin, populate the dict of votes
    if isadmin:
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).where(Nominee.chosen).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (~Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
    else:
        votes = None
    # Count total number of voters
    total = Vote.select(fn.Distinct(Vote.user)).where(~Vote.preliminary).group_by(Vote.user).count()
    # Yay, done
    return render_template('wait.html',
                           nominees=nominees, year=date.today().year,
                           isadmin=isadmin, votes=votes, stage=config.STAGE,
                           total=total,
                           nominations=config.NOMINATIONS, lang=g.lang)
