from www import app
from db import database, Nominee, Vote
from flask import session, url_for, redirect, request, render_template, g, flash
from flask_oauthlib.client import OAuth
from flask_wtf import Form
from wtforms import StringField, HiddenField, TextAreaField, SelectMultipleField, SelectField
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


def merge_dict(target, other):
    for k, v in other.items():
        if isinstance(v, dict):
            node = target.setdefault(k, {})
            merge_dict(node, v)
        else:
            target[k] = v


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
    with codecs.open(os.path.join(config.BASE_DIR, 'lang', 'en.yaml'), 'r', 'utf-8') as f:
        data = yaml.load(f)
        data = data[data.keys()[0]]
    with codecs.open(os.path.join(config.BASE_DIR, 'lang', lang + '.yaml'), 'r', 'utf-8') as f:
        lang_data = yaml.load(f)
        merge_dict(data, lang_data[lang_data.keys()[0]])
    g.lang = data
    g.category_choices = [('', data['choose_category'] + '...')] + [(c, data['nominations'][c]['title']) for c in config.NOMINATIONS]


@app.route('/')
def login():
    if config.STAGE in ('processing', 'results'):
        return wait()
    if config.STAGE in ('call', 'callvote', 'select'):
        return edit_nominees()
    if config.STAGE == 'voting':
        return voting()
    return 'Unknown stage: {0}'.format(config.STAGE)


@app.route('/login')
def login_to_osm():
    if 'osm_token' not in session:
        session['objects'] = request.args.get('objects')
        return openstreetmap.authorize(callback=url_for('oauth'))
    return login()


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
    return redirect(url_for('login'))


class AddNomineeForm(Form):
    who = StringField('Who', validators=[DataRequired()])
    project = TextAreaField('For what')
    url = StringField('URL', validators=[Optional(), URL()])
    category = SelectField('Category', validators=[DataRequired()])
    nomid = HiddenField('Nominee IS', validators=[Optional()])


@app.route('/nominees')
@app.route('/nominees/<cat>')
@app.route('/edit/<edit_id>')
def edit_nominees(cat=None, edit_id=None):
    """Called from login(), a convenience method."""
    uid = session.get('osm_uid', None)
    isadmin = uid in config.ADMINS
    if config.STAGE not in ('call', 'callvote', 'select') and not isadmin:
        return redirect(url_for('login'))
    if cat is None:
        cat = session.get('nomination', 'core')
    if cat == 'all':
        cat = None if isadmin else 'mine'
    if cat == 'mine' and not uid:
        cat = 'core'
    if cat in config.NOMINATIONS or cat is None or cat == 'mine':
        session['nomination'] = cat
    nom = session.get('nomination', cat)

    # Prepare editing form
    edit_obj = None
    if edit_id and uid and (isadmin or config.STAGE in ('call', 'callvote')):
        edit_nom = Nominee.get(Nominee.id == edit_id)
        if (edit_nom.status == Nominee.Status.SUBMITTED and edit_nom.proposedby == uid) or isadmin:
            edit_obj = model_to_dict(edit_nom)
            edit_obj['nomid'] = edit_id
    form = AddNomineeForm(data=edit_obj)
    form.category.choices = g.category_choices

    # Select nominees from the database
    nominees = Nominee.select(Nominee, Vote.user.alias('voteuser')).join(
        Vote, JOIN.LEFT_OUTER, on=(
            (Vote.nominee == Nominee.id) & (Vote.user == uid) & (Vote.preliminary)
        )).order_by(Nominee.id.desc())
    if nom in config.NOMINATIONS:
        nominees = nominees.where(Nominee.category == nom)
    elif nom == 'mine':
        nominees = nominees.where(Nominee.proposedby == uid)
    if nom != 'mine' and not isadmin:
        min_status = (Nominee.Status.SUBMITTED
                      if config.STAGE.startswith('call')
                      else Nominee.Status.ACCEPTED)
        nominees = nominees.where(Nominee.status >= min_status)

    # Calculate the number of votes for the selection team
    if isteam(uid):
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
    else:
        votes = None

    # Prepare a list of categories
    filterables = list(config.NOMINATIONS)
    if uid:
        filterables.insert(0, 'mine')
    if isadmin:
        filterables.insert(0, 'all')

    # All done, return the template
    canadd = isadmin or (uid and config.STAGE.startswith('call') and Nominee.select().where(
        Nominee.proposedby == uid).count() < config.MAX_NOMINEES_PER_USER)
    return render_template('index.html',
                           form=form, nomination=nom or 'all',
                           nominees=nominees.naive(), user=uid, isadmin=isadmin, canvote=canvote(uid),
                           canunvote=config.STAGE == 'callvote' or isteam(uid),
                           votes=votes, statuses={k: v for k, v in Nominee.status.choices},
                           year=config.YEAR, stage=config.STAGE, canadd=canadd,
                           nominations=filterables, lang=g.lang)


@app.route('/add', methods=['POST'])
def add_nominee():
    uid = session.get('osm_uid', None)
    isadmin = uid in config.ADMINS
    if not uid or not (config.STAGE.startswith('call') or isadmin):
        return redirect(url_for('login'))
    form = AddNomineeForm()
    form.category.choices = g.category_choices
    if form.validate():
        if form.nomid.data.isdigit():
            n = Nominee.get(Nominee.id == int(form.nomid.data))
            if n.proposedby != uid and not isadmin:
                return redirect(url_for('edit_nominees'))
        else:
            n = Nominee()
            n.proposedby = session['osm_uid']
            n.status = Nominee.Status.SUBMITTED
        if request.form.get('submit') == g.lang['deletenominee']:
            if n.id:
                n.status = Nominee.Status.DELETED
                n.save()
        else:
            form.populate_obj(n)
            n.save()
        return redirect(url_for('edit_nominees'))
    return 'Error in fields:\n{}'.format('\n'.join(['{}: {}'.format(k, v) for k, v in form.errors.items()]))


@app.route('/delete/<nid>')
def delete_nominee(nid):
    if 'osm_token' not in session or (
            not config.STAGE.startswith('call') and session['osm_uid'] not in config.ADMINS):
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    session['tmp_nominee'] = model_to_dict(n)
    n.delete_instance(recursive=True)
    return redirect(url_for('edit_nominees'))


@app.route('/choose/<nid>')
def choose_nominee(nid):
    if 'osm_token' not in session or session['osm_uid'] not in config.ADMINS:
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    if n.status == Nominee.Status.CHOSEN:
        n.status = Nominee.Status.ACCEPTED
    elif n.status == Nominee.Status.ACCEPTED:
        n.status = Nominee.Status.CHOSEN
    else:
        raise Exception('Cannot choose non-accepted nominee')
    n.save()
    return redirect(url_for('edit_nominees'))

@app.route('/setstatus/<nid>')
@app.route('/setstatus/<nid>/<status>')
def set_status(nid, status=None):
    if 'osm_token' not in session or session['osm_uid'] not in config.ADMINS or status is None:
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    n.status = status
    n.save()
    return redirect(url_for('edit_nominees'))

def canvote(uid):
    if 'osm_token' not in session:
        return False
    if session['osm_uid'] in config.ADMINS:
        return True
    if config.STAGE != 'callvote' and not isteam(uid):
        return False
    return Vote.select().join(Nominee).where(
        (Vote.user == uid) & (Vote.preliminary) & (Nominee.category == session['nomination'])).count() < 5


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
    nominees = Nominee.select().where(Nominee.status == Nominee.Status.CHOSEN)
    return render_template('list.html',
                           nominees=nominees, year=date.today().year,
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/voting')
def voting():
    """Called from login(), a convenience method."""
    if 'osm_token' not in session:
        return redirect(url_for('login_to_osm'))
    if config.STAGE != 'voting':
        return redirect(url_for('login'))

    uid = session['osm_uid']
    isadmin = uid in config.ADMINS
    nominees_list = Nominee.select(Nominee, Vote.user.alias('voteuser')).where(Nominee.status == Nominee.Status.CHOSEN).join(
        Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.user == uid) & (~Vote.preliminary))).naive()
    # Shuffle the nominees
    nominees = [n for n in nominees_list]
    rnd = Random()
    rnd.seed(uid)
    rnd.shuffle(nominees)
    # Make a dict of categories user voted in
    cats = set([x.category for x in Nominee.select(Nominee.category).join(Vote, JOIN.INNER, on=((Vote.nominee == Nominee.id) & (~Vote.preliminary) & (Vote.user == uid))).distinct()])
    # For admin, populate the dict of votes
    if isadmin:
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).where(Nominee.status == Nominee.Status.CHOSEN).join(
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
                           total=total, voted_cats=cats,
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/votes', methods=['POST'])
def vote_all():
    if 'osm_token' not in session or config.STAGE != 'voting':
        return redirect(url_for('login'))
    uid = session['osm_uid']
    # Delete current votes to replace by with the new ones
    q = Vote.delete().where((Vote.user == uid) & (~Vote.preliminary))
    q.execute()
    for nom in config.NOMINATIONS:
        votes = request.form.getlist('vote_{}'.format(nom))
        for vote in votes:
            print('{}: {}'.format(nom, vote))
            v = Vote()
            v.nominee = Nominee.get(Nominee.id == int(vote))
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
            Nominee.category == n.category).get()
        v.delete_instance()
    except Vote.DoesNotExist:
        pass
    v = Vote()
    v.nominee = n
    v.user = uid
    v.preliminary = False
    v.save()
    return redirect(url_for('voting'))


@app.route('/results')
def wait():
    uid = session['osm_uid'] if 'osm_uid' in session else 0
    isadmin = uid in config.ADMINS
    nominees = Nominee.select(Nominee, Vote.user.alias('voteuser')).where(Nominee.status == Nominee.Status.CHOSEN).join(
        Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.user == uid) & (~Vote.preliminary))).naive()
    # For admin, populate the dict of votes
    winners = [[0, 0] for x in range(len(config.NOMINATIONS))]
    if isadmin or config.STAGE == 'results':
        votesq = Nominee.select(Nominee.id, Nominee.category, fn.COUNT(Vote.id).alias('num_votes')).where(Nominee.status == Nominee.Status.CHOSEN).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (~Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
            if v.num_votes > winners[v.category][1]:
                winners[v.category] = (v.id, v.num_votes)
    else:
        votes = None
    # Count total number of voters
    total = Vote.select(fn.Distinct(Vote.user)).where(~Vote.preliminary).group_by(Vote.user).count()
    # Update a link in the description
    desc = g.lang['stages'][config.STAGE]['description']
    desc = desc.replace('{', '<a href="{}">'.format(url_for('static', filename='osmawards2016.txt'))).replace('}', '</a>')
    # Yay, done
    return render_template('wait.html',
                           nominees=nominees, year=date.today().year,
                           description=desc,
                           isadmin=isadmin, votes=votes, stage=config.STAGE,
                           total=total, winners=winners, isresults=config.STAGE == 'results',
                           nominations=config.NOMINATIONS, lang=g.lang)
