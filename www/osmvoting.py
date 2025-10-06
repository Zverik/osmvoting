from . import app
from .db import database, Nominee, Vote
from flask import session, url_for, redirect, request, render_template, g, flash, Response
from authlib.integrations.flask_client import OAuth
from authlib.common.errors import AuthlibBaseError
from flask_wtf import FlaskForm
from functools import wraps
from wtforms import (
    StringField, HiddenField, TextAreaField, SelectMultipleField,
    SelectField, URLField,
)
from wtforms.validators import DataRequired, Optional, URL
from playhouse.shortcuts import model_to_dict
from random import Random, shuffle
from peewee import JOIN, fn
from xml.etree import ElementTree as etree
from io import StringIO
import yaml
import os
import config

oauth = OAuth(app)
oauth.register(
    'openstreetmap',
    api_base_url='https://api.openstreetmap.org/api/0.6/',
    access_token_url='https://www.openstreetmap.org/oauth2/token',
    authorize_url='https://www.openstreetmap.org/oauth2/authorize',
    client_id=app.config['OAUTH_KEY'] or '123',
    client_secret=app.config['OAUTH_SECRET'] or '123',
    client_kwargs={'scope': 'read_prefs'},
)


@app.before_request
def before_request():
    database.connect()
    load_user_language()


@app.teardown_request
def teardown(exception):
    if not database.is_closed():
        database.close()


def get_user(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in g:
            if 'osm_token2' in session:
                g.user_id = session['osm_uid']
                g.is_admin = g.user_id in config.ADMINS
                g.is_team = (config.STAGE == 'select' and
                             g.user_id in config.TEAM)
            else:
                g.user_id = None
                g.is_admin = False
                g.is_team = False
        return f(*args, **kwargs)
    return decorated


def merge_dict(target, other):
    for k, v in other.items():
        if isinstance(v, dict):
            node = target.setdefault(k, {})
            merge_dict(node, v)
        else:
            target[k] = v


def load_language(path, lang):
    with open(os.path.join(
            config.BASE_DIR, path, 'en.yaml'), 'r') as f:
        data = yaml.safe_load(f)
        data = data[list(data.keys())[0]]
    lang_file = os.path.join(config.BASE_DIR, path, lang + '.yaml')
    if os.path.exists(lang_file):
        with open(lang_file, 'r') as f:
            lang_data = yaml.safe_load(f)
            merge_dict(data, lang_data[list(lang_data.keys())[0]])
    return data


def load_user_language():
    supported = set([x[:x.index('.')] for x in os.listdir(
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

    data = load_language('lang', lang)
    descs = load_language('lang/descriptions', lang)
    data['desc'] = descs
    g.lang = data
    g.category_choices = ([('', data['choose_category'] + '...')] +
                          [(c, data['nominations'][c]['title']) for c in config.NOMINATIONS])


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
    if 'osm_token2' not in session:
        session['objects'] = request.args.get('objects')
        return oauth.openstreetmap.authorize_redirect(
            url_for('oauth_callback', _external=True))
    return login()


@app.route('/oauth')
def oauth_callback():
    try:
        token = oauth.openstreetmap.authorize_access_token()
    except AuthlibBaseError:
        return 'Denied. <a href="' + url_for('login') + '">Try again</a>.'

    session['osm_token2'] = token

    response = oauth.openstreetmap.get('user/details')
    user_details = etree.fromstring(response.content)
    session['osm_uid'] = int(user_details[0].get('id'))
    return redirect(url_for('login'))


@app.route('/logout')
def logout():
    if 'osm_token2' in session:
        del session['osm_token2']
    if 'osm_uid' in session:
        del session['osm_uid']
    return redirect(url_for('login'))


class AddNomineeForm(FlaskForm):
    who = StringField('Who', validators=[DataRequired()])
    project = TextAreaField('For what')
    url = URLField('URL', validators=[Optional(), URL()])
    category = SelectField('Category', validators=[DataRequired()])
    nomid = HiddenField('Nominee IS', validators=[Optional()])


@app.route('/nominees')
@app.route('/nominees/<cat>')
@app.route('/edit/<edit_id>')
@get_user
def edit_nominees(cat=None, edit_id=None):
    """Called from login(), a convenience method."""
    if config.STAGE not in ('call', 'callvote', 'select') and not g.is_admin:
        return redirect(url_for('login'))
    if cat is None:
        cat = session.get('nomination', 'core')
    if cat == 'all':
        cat = None if g.is_admin else 'mine'
    if cat == 'mine' and not g.user_id:
        cat = 'core'
    if cat in config.NOMINATIONS or cat is None or cat == 'mine':
        session['nomination'] = cat
    nom = session.get('nomination', cat)

    # Prepare editing form
    edit_obj = None
    if edit_id and g.user_id and (g.is_admin or config.STAGE in ('call', 'callvote')):
        edit_nom = Nominee.get(Nominee.id == edit_id)
        if (edit_nom.status == Nominee.Status.SUBMITTED and edit_nom.proposedby == g.user_id) or g.is_admin:
            edit_obj = model_to_dict(edit_nom)
            edit_obj['nomid'] = edit_id
    form = AddNomineeForm(data=edit_obj)
    form.category.choices = g.category_choices

    # Select nominees from the database
    nominees = Nominee.select(Nominee, Vote.user.alias('voteuser')).join(
        Vote, JOIN.LEFT_OUTER, on=(
            (Vote.nominee == Nominee.id) & (Vote.user == g.user_id) & (Vote.preliminary)
        )).order_by(Nominee.id.desc())

    if nom in config.NOMINATIONS:
        nominees = nominees.where(Nominee.category == nom)
    elif nom == 'mine':
        nominees = nominees.where(Nominee.proposedby == g.user_id)
    if nom != 'mine' and not g.is_admin:
        min_status = (Nominee.Status.SUBMITTED
                      if config.STAGE in ('call', 'callvote', 'select')
                      else Nominee.Status.ACCEPTED)
        nominees = nominees.where(Nominee.status >= min_status)

    # Calculate the number of votes for the selection team
    if g.is_team:
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
    else:
        votes = None

    # Prepare a list of categories
    filterables = list(config.NOMINATIONS)
    if g.user_id:
        filterables.insert(0, 'mine')
    if g.is_admin:
        filterables.insert(0, 'all')

    # All done, return the template
    canadd = g.is_admin or (g.user_id and config.STAGE.startswith('call') and Nominee.select().where(
        Nominee.proposedby == g.user_id).count() < config.MAX_NOMINEES_PER_USER)
    return render_template('index.html',
                           form=form, nomination=nom or 'all',
                           nominees=nominees.objects(), user=g.user_id, isadmin=g.is_admin,
                           canvote=canvote(g.user_id),
                           canunvote=config.STAGE == 'callvote' or g.is_team,
                           votes=votes, statuses={k: v for k, v in Nominee.status.choices},
                           stage=config.STAGE, canadd=canadd,
                           nominations=filterables, lang=g.lang)


@app.route('/add', methods=['POST'])
@get_user
def add_nominee():
    if not g.user_id or not (config.STAGE.startswith('call') or g.is_admin):
        return redirect(url_for('login'))
    form = AddNomineeForm()
    form.category.choices = g.category_choices
    if form.validate():
        if form.nomid.data.isdigit():
            n = Nominee.get(Nominee.id == int(form.nomid.data))
            if n.proposedby != g.user_id and not g.is_admin:
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
    else:
        flash('Error in fields:\n{}'.format(
            '\n'.join(['{}: {}'.format(k, v)
                       for k, v in form.errors.items()])))
    return redirect(url_for('edit_nominees'))


@app.route('/delete/<nid>')
@get_user
def delete_nominee(nid):
    if not g.user_id or (
            not config.STAGE.startswith('call') and not g.is_admin):
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    session['tmp_nominee'] = model_to_dict(n)
    n.delete_instance(recursive=True)
    return redirect(url_for('edit_nominees'))


@app.route('/choose/<nid>')
@get_user
def choose_nominee(nid):
    if not g.is_admin:
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    if n.status == Nominee.Status.CHOSEN:
        n.status = Nominee.Status.ACCEPTED
    elif n.status == Nominee.Status.ACCEPTED:
        n.status = Nominee.Status.CHOSEN
    else:
        flash('Cannot choose non-accepted nominee')
        return redirect(url_for('edit_nominees'))
    n.save()
    return redirect(url_for('edit_nominees'))


@app.route('/setstatus/<nid>')
@app.route('/setstatus/<nid>/<status>')
@get_user
def set_status(nid, status=None):
    if status is None or not g.is_admin:
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    n.status = status
    n.save()
    return redirect(url_for('edit_nominees'))


@get_user
def canvote(uid):
    if not g.user_id:
        return False
    if g.is_admin:
        return True
    if config.STAGE != 'callvote' and not g.is_team:
        return False
    return Vote.select().join(Nominee).where(
        (Vote.user == uid) & (Vote.preliminary) &
        (Nominee.category == session['nomination'])).count() < 5


@app.route('/prevote/<nid>')
@get_user
def prevote(nid):
    if config.STAGE != 'call' and not g.is_team:
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    try:
        v = Vote.get((Vote.user == g.user_id) & (Vote.nominee == n) & (Vote.preliminary))
        v.delete_instance()
    except Vote.DoesNotExist:
        if canvote(g.user_id):
            v = Vote()
            v.nominee = n
            v.user = g.user_id
            v.preliminary = True
            v.save()
    return redirect(url_for('edit_nominees'))


@app.route('/list')
@get_user
def list_chosen():
    nominees = Nominee.select().where(Nominee.status == Nominee.Status.CHOSEN)
    return render_template('list.html',
                           nominees=nominees, user=g.user_id,
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/voting')
@get_user
def voting():
    """Called from login(), a convenience method."""
    if not g.user_id:
        return redirect(url_for('list_chosen'))
    if config.STAGE != 'voting':
        return redirect(url_for('login'))

    nominees_list = Nominee.select(Nominee, Vote.user.alias('voteuser')).where(Nominee.status == Nominee.Status.CHOSEN).join(
        Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.user == g.user_id) & (~Vote.preliminary))).objects()
    # Shuffle the nominees
    nominees = [n for n in nominees_list]
    rnd = Random()
    rnd.seed(g.user_id)
    rnd.shuffle(nominees)
    # Make a dict of categories user voted in
    cats = set([x.category for x in Nominee.select(Nominee.category).join(Vote, JOIN.INNER, on=((Vote.nominee == Nominee.id) & (~Vote.preliminary) & (Vote.user == g.user_id))).distinct()])
    # For admin, populate the dict of votes
    if g.is_admin:
        votesq = Nominee.select(Nominee.id, fn.COUNT(Vote.id).alias('num_votes')).where(Nominee.status == Nominee.Status.CHOSEN).join(
            Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (~Vote.preliminary))).group_by(Nominee.id)
        votes = {}
        for v in votesq:
            votes[v.id] = v.num_votes
    else:
        votes = None
    # Count total number of voters
    total = Vote.select(fn.Distinct(Vote.user)).where(~Vote.preliminary).group_by(Vote.user).count()
    readmore = (g.lang['stages']['voting']['readmore']
                .replace('{', '<a href="{}">'.format(
                    g.lang['stages']['voting']['readmore_link']))
                .replace('}', '</a>'))
    # Yay, done
    return render_template('voting.html',
                           nominees=nominees,
                           isadmin=g.is_admin, votes=votes, stage=config.STAGE,
                           total=total, voted_cats=cats, readmore=readmore,
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/votes', methods=['POST'])
@get_user
def vote_all():
    if not g.user_id or config.STAGE != 'voting':
        return redirect(url_for('login'))
    # Delete current votes to replace by with the new ones
    q = Vote.delete().where((Vote.user == g.user_id) & (~Vote.preliminary))
    q.execute()
    for nom in config.NOMINATIONS:
        votes = request.form.getlist('vote_{}'.format(nom))
        for vote in votes:
            v = Vote()
            v.nominee = Nominee.get(Nominee.id == int(vote))
            v.user = g.user_id
            v.preliminary = False
            v.save()
    flash(g.lang['thanksvoted'])
    return redirect(url_for('voting'))


@app.route('/vote/<nid>')
@get_user
def vote(nid):
    if not g.user_id or config.STAGE != 'voting':
        return redirect(url_for('login'))
    n = Nominee.get(Nominee.id == nid)
    try:
        # Delete votes from the same category by this voter
        v = Vote.select().where((Vote.user == g.user_id) & (~Vote.preliminary)).join(Nominee).where(
            Nominee.category == n.category).get()
        v.delete_instance()
    except Vote.DoesNotExist:
        pass
    v = Vote()
    v.nominee = n
    v.user = g.user_id
    v.preliminary = False
    v.save()
    return redirect(url_for('voting'))


@app.route('/results')
@get_user
def wait():
    nominees = Nominee.select(Nominee, Vote.user.alias('voteuser')).where(Nominee.status == Nominee.Status.CHOSEN).join(
        Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (Vote.user == g.user_id or 0) & (~Vote.preliminary))).objects()
    # For admin, populate the dict of votes
    winners = {x: [0, 0] for x in config.NOMINATIONS}
    if g.is_admin or config.STAGE == 'results':
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
    desc = desc.replace('{', '<a href="{}">'.format(url_for('dump_votes'))).replace('}', '</a>')
    # Yay, done
    return render_template('wait.html',
                           nominees=nominees,
                           description=desc,
                           isadmin=g.is_admin, votes=votes, stage=config.STAGE,
                           total=total, winners=winners, isresults=config.STAGE == 'results',
                           nominations=config.NOMINATIONS, lang=g.lang)


@app.route('/votes.txt')
@get_user
def dump_votes():
    if not g.is_admin and config.STAGE != 'results':
        return redirect(url_for('login'))

    result = StringIO()

    # Get a list of nominees
    nq = Nominee.select().where(Nominee.status == Nominee.Status.CHOSEN)
    nominees = {nom: {} for nom in config.NOMINATIONS}
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
            users[v.user] = {nom: [] for nom in config.NOMINATIONS}
        n = allnoms[v.nominee.id]
        users[v.user][n['nom']].append(n['pos'])
        nominees[n['nom']][v.nominee.id]['votes'] += 1

    # Print that list nicely
    for nom in config.NOMINATIONS:
        print(nom, file=result)
        for n in sorted(nominees[nom].values(), key=lambda x: x['pos']):
            print('- {}: {}'.format(n['who'], n['votes']), file=result)
        print('', file=result)

    # Print the result randomized by user ids
    v = list(users.values())
    shuffle(v)
    for user in v:
        print(','.join([''.join([str(x+1) for x in sorted(user[nom])])
                        for nom in config.NOMINATIONS]), file=result)

    return Response(result.getvalue(), mimetype='text/plain')


@app.route('/wiki.txt')
@get_user
def dump_wiki():
    if not g.is_admin and config.STAGE != 'results':
        return redirect(url_for('login'))

    result = StringIO()
    nominees = Nominee.select().where(Nominee.status == Nominee.Status.CHOSEN)
    votesq = Nominee.select(Nominee.id, Nominee.category, fn.COUNT(Vote.id).alias('num_votes')).where(Nominee.status == Nominee.Status.CHOSEN).join(
        Vote, JOIN.LEFT_OUTER, on=((Vote.nominee == Nominee.id) & (~Vote.preliminary))).group_by(Nominee.id)
    votes = {}
    for v in votesq:
        votes[v.id] = v.num_votes

    for nom in config.NOMINATIONS:
        print('', file=result)
        print('== {} =='.format(g.lang['nominations'][nom]['title']), file=result)
        print('', file=result)
        for n in nominees:
            lst = []
            if n.category == nom:
                lst.append({
                    'votes': votes[n.id],
                    'first': '; {} [{}]'.format(n.who, votes[n.id]),
                    'second': ': {}'.format(n.project) if not n.url
                    else ': {} [{}]'.format(n.project, n.url),
                })
            for line in sorted(lst, key=lambda k: -k['votes']):
                print(line['first'], file=result)
                print(line['second'], file=result)

    return Response(result.getvalue(), mimetype='text/plain')
