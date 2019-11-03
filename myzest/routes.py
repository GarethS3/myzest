from flask import render_template, request, jsonify, redirect, flash, session
from myzest import app, mongo, bcrypt
from bson.objectid import ObjectId
import re
import json
import math
from datetime import date
from os import path, remove as os_remove

rcp = {
        'difficulties': ("easy", "average", "hard"),
        'sortings': (("name", "Name"), ("updated", "Date"), ("favorite", "Popularity"), ("views", "Viewed"),
                     ("time.total", "Time"), ("serves", "Servings")),
        'foodTypes': mongo.db.foodtype.distinct("name"),
        'foodCategories': mongo.db.category.distinct("name")
    }
rcp['foodTypes'].sort()
rcp['foodCategories'].sort()

pic_extensions = ("jpg", "jpeg", "png", "gif")


# Override to serialize ObjectIds data from DB
# into str for user's session object
class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)
        return json.JSONEncoder.default(self, o)


@app.template_filter()
def min_to_hour(time):
    h = time // 60
    m = time % 60
    return str(h) + "h" + str(m) if h > 0 else str(m)


@app.template_filter()
def oid_date(time):
    return time.date()


def formdata_to_query(data):
    """ Processes data from the search form to build query with relevant fields only"""
    # time and serves
    try:
        time = {
            "$gte": int(data.pop("timer.start")),
            "$lte": int(data.pop("timer.stop"))
        }
        serves = {
            '$gte': int(data.pop('serve.start')),
            '$lte': int(data.pop('serve.stop'))
        }
    except(KeyError):
        time = {
            "$gte": int(5),
            "$lte": int(240)
        }
        serves = {
            '$gte': int(1),
            '$lte': int(20)
        }
    # text
    try:
        if data['textSearch'] == '':
            text_search = False
            del data['textSearch']
        else:
            text_search = True
            words = {'$search': data.pop('textSearch')}
    except(KeyError):
        text_search = False
    query = {k: v for (k, v) in data.items() if data[k] not in ["any", ""]}
    query['serves'] = serves
    query['time.total'] = time

    if text_search:
        query['$text'] = words

    return query


def make_query(requested_data):
    """ Builds mongoDB query from form data posted to search_recipes
    and stores into session 'search' dict """

    data = requested_data.to_dict()

    sort = {data.pop("sort"): -1} if data['sort'] in ['favorite', 'views', 'updated'] else {data.pop("sort"): 1}

    query = formdata_to_query(data)

    session['search'] = {'query': query,
                         'sort': sort}

    return query, sort


class Paginate:
    """ Queries MongoDB for recipes and builds pagination """

    per_page = 6

    def __init__(self, query, sort, target_page=1):
        self.total_pages = math.ceil(mongo.db.recipes.find(query).count() / self.per_page)
        self.current = target_page
        self.to_skip = self.per_page * (self.current - 1)
        self.recipes = mongo.db.recipes.aggregate([
            {'$match': query},
            {'$sort': sort},
            {'$skip': self.to_skip},
            {'$limit': self.per_page},
            {'$lookup': {
                'from': 'users',
                'localField': 'author_id',
                'foreignField': '_id',
                'as': 'author'
            }},
            {'$unwind': {'path': '$author'}},
            {'$addFields': {'avatar': '$author.avatar'}},
            {'$project': {'author': 0}}
        ])

    def get_page(self):
        return self.recipes


def hash_password(password):
    return bcrypt.generate_password_hash(password).decode('utf-8')


@app.route('/')
@app.route('/home')
def home():
    # store recipe criterias
    session['rcp'] = rcp

    if 'views' not in session:
        session['views'] = []

    top_faved = mongo.db.recipes.aggregate([
        {'$lookup': {
            'from': 'users',
            'localField': 'author_id',
            'foreignField': '_id',
            'as': 'author'
        }},
        {'$unwind': {'path': '$author'}},
        {'$addFields': {'avatar': '$author.avatar'}},
        {'$project': {'author': 0}},
        {'$sort': {'favorite': -1}},
        {'$limit': 5}
    ])
    latests = mongo.db.recipes.aggregate([
        {'$lookup': {
            'from': 'users',
            'localField': 'author_id',
            'foreignField': '_id',
            'as': 'author'
        }},
        {'$unwind': {'path': '$author'}},
        {'$addFields': {'avatar': '$author.avatar'}},
        {'$project': {'author': 0}},
        {'$sort': {'updated': -1}},
        {'$limit': 5}
    ])

    query = session['search']['query'] if 'search' in session else {}
    sort = session['search']['sort'] if 'search' in session else session['rcp']['sortings']

    return render_template('home.html', latests=latests, top_faved=top_faved,
                           query=query, sort=sort)


@app.route('/recipe/<recipe_id>')
def get_recipe(recipe_id):
    recipe = mongo.db.recipes.find_one({'_id': ObjectId(recipe_id)})
    author = mongo.db.users.find_one({'_id': ObjectId(recipe['author_id'])})
    if recipe_id not in session['views']:
        session['views'].append(recipe_id)
        session.modified = True
        if 'user' not in session or session['user']['_id'] != str(author['_id']):
            mongo.db.recipes.update({'_id': ObjectId(recipe_id)}, {'$inc': {'views': 1}})
    return render_template('recipe.html', recipe=recipe, author=author, session=session)


@app.route('/register')
def register():
    if 'user' in session:
        flash('{}, you are already logged in'.format(session['user']['username']), 'info')
        return redirect('home')
    return render_template('register.html')


@app.route('/add_user', methods=['POST'])
def add_user():
    data = request.form.to_dict()
    new_user = {
        'username': data['username'].title(),
        'email': data['email'].lower(),
        'password': hash_password(data['password']),
        'favorites': [],
        'avatar': "default.png"
    }
    user_in_db = mongo.db.users.find_one({"$or": [{"username": new_user["username"]}, {"email": new_user["email"]}]})
    if user_in_db:
        flash('This user already exists', 'warning')
    elif user_in_db is None:
        registered_user = mongo.db.users.insert_one(new_user)
        # user session object keeps only _id, name and favorites for interaction
        session['user'] = {
            'username': new_user['username'],
            '_id': str(registered_user.inserted_id),
            'favorites': new_user['favorites']
        }
        flash('Welcome {} ! Your account was created with {}'
              .format(new_user['username'], new_user['email']), 'success')
    return redirect('home')


@app.route('/check_user', methods=['POST'])
def check_user():
    data = request.get_json()
    if data['field'] == 'username':
        value = data['value'].title()
    else:
        value = data['value'].lower()
    document = bool(mongo.db.users.find_one({data['field']: value}))

    if data['form'] == "registration":
        return jsonify({'error': "This {} is already taken".format(data['field'])}) if document else "success"
    elif data['form'] == 'editprofile':
        session_user = mongo.db.users.find_one({'username': session['user']['username']})
        return jsonify({'error': "This {} is already used by someone else".format(data['field'])}) if document and session_user[data['field']] != value else "success"
    else:
        return "success" if document else jsonify({'error': "This {} is not registered".format(data['field'])})


@app.route('/login')
def login():
    if 'user' in session:
        flash('{}, you are already logged in'.format(session['user']['username']), 'info')
        return redirect('home')
    return render_template('login.html')


@app.route('/log_usr', methods=['POST'])
def log_usr():
    data = request.form.to_dict()
    user_in_db = mongo.db.users.find_one({'email': data['email'].lower()})
    if user_in_db and bcrypt.check_password_hash(user_in_db['password'], data['password']):
        user = mongo.db.users.find_one({'_id': user_in_db['_id']}, {'username': 1, 'favorites': 1})
        user = JSONEncoder().encode(user)
        session['user'] = json.loads(user)
        views = session['views']
        for viewed in views:
            if 'recipes' in user_in_db:
                for r in user_in_db['recipes']:
                    if str(r) == viewed:
                        mongo.db.recipes.update({'_id': ObjectId(viewed)}, {'$inc': {'views': -1}})
        flash('Welcome back {} !'.format(user_in_db['username']), 'success')
        return redirect('home')
    elif user_in_db and not bcrypt.check_password_hash(user_in_db['password'], data['password']):
        flash('Login unsuccessful. Please check email and password provided', 'warning')
        return redirect('login')
    elif not user_in_db:
        flash('Login unsuccessful. Please check email', 'warning')
        return render_template('login.html')


@app.route('/logout')
def logout():
    if 'user' in session:
        username = session['user']['username']
        session.pop('user')
        flash('We hope to see you soon {}'.format(username), 'info')
    else:
        flash('You are not logged in', 'warning')
    return redirect('home')


@app.route('/addrecipe')
def add_recipe():
    if 'user' not in session:
        flash('To add recipes, you need to login first', 'warning')
        return redirect('login')
    return render_template('addrecipe.html')


@app.route('/insertrecipe', methods=['GET', 'POST'])
def insert_recipe():
    # Keep checking for active valid connection
    if 'user' not in session:
        flash('You are currently not logged in', 'warning')
        return redirect('login')

    # Create recipe Obj with required details
    data = request.form.to_dict()
    new_recipe = dict()

    # Main required recipe details
    new_recipe['author_id'] = ObjectId(session['user']['_id'])
    new_recipe['name'] = data.pop('name')
    new_recipe['description'] = data.pop('description')
    new_recipe['difficulty'] = data.pop('difficulty')
    new_recipe['serves'] = int(data.pop('serves'))
    new_recipe['time'] = {"total": int(data.pop('time'))}
    # initial views
    new_recipe['views'] = 0
    # add time creation/update
    new_recipe['updated'] = date.today().isoformat()

    # Add Ingredients
    match_ingr = re.compile("ingredient-")
    match_amt = re.compile("amount-")
    ingredients = [data[entry] for entry in sorted(data.keys()) if match_ingr.match(entry)]
    amount = [data[entry] for entry in sorted(data.keys()) if match_amt.match(entry)]
    ing_list = []
    for i in range(len(ingredients)):
        ing_list.append({"name": ingredients[i],
                         "amount": amount[i]})
    new_recipe['ingredients'] = ing_list

    # Add Steps
    match_step = re.compile("step-")
    steps = [data[entry] for entry in sorted(data.keys()) if match_step.match(entry)]
    # build list of dict to add image input later
    new_recipe['steps'] = [{"description": step} for step in steps]

    # Optional recipe details
    if "foodType" in data and data['foodType'] != "":
        new_recipe['foodType'] = data.pop('foodType')

    rcp = mongo.db.recipes.insert_one(new_recipe)

    # Add recipe's id to user's recipe list
    mongo.db.users.update({'_id': ObjectId(session['user']['_id'])}, {'$push': {'recipes': rcp.inserted_id}})

    # Rename and store image using recipe's id
    pic = request.files['img']
    file_ext = pic.filename.rsplit('.', 1)[-1].lower()
    filename = str(rcp.inserted_id) + '.' + file_ext
    # double check file extension
    if not filename.endswith(pic_extensions):
        flash('wrong file extension', 'warning')
        return redirect('addrecipe')
    else:
        pic.save(path.join(app.config['RECIPE_PIC_DIR'], filename))

    # update recipe on db with image filename
    mongo.db.recipes.update({'_id': rcp.inserted_id}, {'$set': {'image': filename}})

    return redirect('/recipe/{}'.format(rcp.inserted_id))


@app.route('/deleterecipe/<recipe_id>')
def delete_recipe(recipe_id):
    recipe_id = recipe_id
    mongo.db.users.update({"_id": ObjectId(session['user']['_id'])}, {'$pull': {'recipes': ObjectId(recipe_id)}})
    os_remove(path.join(app.config['RECIPE_PIC_DIR'], mongo.db.recipes.find_one({'_id': ObjectId(recipe_id)})['image']))
    mongo.db.recipes.remove({"_id": ObjectId(recipe_id)})
    mongo.db.users.update_many(
        {'favorites': {'$elemMatch': {'$eq': ObjectId(recipe_id)}}},
        {'$pull': {'favorites': ObjectId(recipe_id)}})
    return redirect('/home')


@app.route('/editrecipe/<recipe_id>', methods=['GET', 'POST'])
def edit_recipe(recipe_id):
    this_recipe = mongo.db.recipes.find_one({'_id': ObjectId(recipe_id)})
    if request.method == 'POST':
        # Keep checking for active valid connection
        if 'user' not in session:
            flash('Sorry the connection was lost, please login', 'warning')
            return redirect('login')

        # Create recipe Obj with main required details
        data = request.form.to_dict()
        upd_recipe = dict()

        upd_recipe['author_id'] = ObjectId(session['user']['_id'])
        upd_recipe['name'] = data.pop('name')
        upd_recipe['description'] = data.pop('description')
        upd_recipe['difficulty'] = data.pop('difficulty')
        upd_recipe['serves'] = int(data.pop('serves'))
        upd_recipe['time'] = {"total": int(data.pop('time'))}
        upd_recipe['views'] = this_recipe['views']

        # Add Ingredients
        match_ingr = re.compile("ingredient-")
        match_amt = re.compile("amount-")
        ingredients = [data[entry] for entry in sorted(data.keys()) if match_ingr.match(entry)]
        amount = [data[entry] for entry in sorted(data.keys()) if match_amt.match(entry)]
        ing_list = []
        for i in range(len(ingredients)):
            ing_list.append({"name": ingredients[i],
                             "amount": amount[i]})
        upd_recipe['ingredients'] = ing_list

        # Add Steps
        match_step = re.compile("step-")
        steps = [data[entry] for entry in sorted(data.keys()) if match_step.match(entry)]
        # build list of dict to add image input later
        upd_recipe['steps'] = [{"description": step} for step in steps]

        # update last modified
        upd_recipe['updated'] = date.today().isoformat()

        # Optional recipe details
        if data['foodType'] != "none":
            upd_recipe['foodType'] = data.pop('foodType')

        # Keep image file from previous version
        pic = request.files['img'].filename
        if pic != "":
            file_ext = pic.rsplit('.', 1)[-1].lower()
            filename = str(this_recipe['_id']) + '.' + file_ext
            # double check for valid file extension
            if not filename.endswith(pic_extensions):
                flash('wrong file extension', 'warning')
                return redirect('/editrecipe/{}'.format(recipe_id))
            else:
                upd_recipe['image'] = filename
                request.files['img'].save(path.join(app.config['RECIPE_PIC_DIR'], filename))
        else:
            upd_recipe['image'] = this_recipe['image']

        # Update this recipe on DB
        mongo.db.recipes.replace_one({'_id': this_recipe['_id']}, upd_recipe)

        return redirect('/recipe/{}'.format(recipe_id))

    return render_template('editrecipe.html', recipe=this_recipe)


@app.route('/favme', methods=['POST'])
def favme():
    data = request.get_json()

    faved = session['user']['favorites']

    if data['recipe_id'] in faved:
        mongo.db.users.update({'_id': ObjectId(data['user_id'])},
                              {'$pull': {'favorites': ObjectId(data['recipe_id'])}})
        mongo.db.recipes.update({'_id': ObjectId(data['recipe_id'])},
                                {'$inc': {'favorite': -1}})

        faved.remove(data['recipe_id'])
        session['user']['favorites'] = faved
        # apply modification to session object
        session.modified = True
        return jsonify({"message": "removed"})

    elif data['recipe_id'] not in faved:
        mongo.db.users.update({'_id': ObjectId(data['user_id'])},
                              {'$push': {'favorites': ObjectId(data['recipe_id'])}})
        mongo.db.recipes.update({'_id': ObjectId(data['recipe_id'])},
                                {'$inc': {'favorite': 1}})

        faved.append(data['recipe_id'])
        session['user']['favorites'] = faved
        # apply modification to session object
        session.modified = True
        return jsonify({"message": "added"})
    else:
        return jsonify({"message": "Operation error"})


@app.route('/searchrecipes', methods=['GET', 'POST'])
def search_recipes():
    """ Queries DB and paginate results;
     End point is first accessed with POST request which stores query and sort in session object """

    if request.method == "POST":

        query, sort = make_query(request.form)
        paginate = Paginate(query, sort)

    elif request.method == 'GET':
        query = session['search']['query']
        sort = session['search']['sort']

        paginate = Paginate(query, sort, int(request.args['target_page']))

    results = paginate.get_page()

    return render_template('results.html',
                           query=query,
                           sort=sort,
                           results=results,
                           paginate=paginate)


@app.route('/searchcount', methods=['POST'])
def searchcount():
    data = request.get_json()
    query = formdata_to_query(data)
    nbr_recipes = mongo.db.recipes.find(query).count()
    return jsonify({"nbr_recipes": nbr_recipes})


@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/contact')
def contact():
    if 'user' in session:
        user_email = mongo.db.users.distinct('email', {'_id': ObjectId(session['user']['_id'])})[0]
        return render_template('contact.html', user=session['user'], email=user_email)
    return render_template('contact.html')


@app.route('/profile/<profile_id>')
def profile(profile_id):
    recipes = mongo.db.recipes.aggregate([
        {'$match': {'author_id': ObjectId(profile_id)}},
        {'$lookup': {
            'from': 'users',
            'localField': 'author_id',
            'foreignField': '_id',
            'as': 'author'
        }},
        {'$unwind': {'path': '$author'}},
        {'$addFields': {'avatar': '$author.avatar'}},
        {'$project': {'author': 0}}
    ])

    full_profile = mongo.db.users.aggregate([
        {'$match': {'_id': ObjectId(profile_id)}},
        {'$lookup': {'from': 'recipes',
                     'pipeline': [
                         {'$match': {'author_id': ObjectId(profile_id)}},
                         {'$group': {'_id': '$author_id',
                                     'faved': {'$sum': '$favorite'}
                                     }
                          }
                     ],
                     'as': 'recipefaved'
                     }
         },
        {'$replaceRoot': {'newRoot': {'$mergeObjects': [{'$arrayElemAt': ['$recipefaved', 0]}, '$$ROOT']}}},
        {'$project': {'recipefaved': 0}}
    ])
    profile = list(full_profile)[0]

    from_favorite = [
        mongo.db.recipes.aggregate([
            {'$match': {'_id': recipe}},
            {'$lookup': {
                'from': 'users',
                'localField': 'author_id',
                'foreignField': '_id',
                'as': 'author'
            }},
            {'$unwind': {'path': '$author'}},
            {'$addFields': {'avatar': '$author.avatar'}},
            {'$project': {'author': 0}}
        ]) for recipe in profile['favorites']
    ]
    faved = [recipe for cursor in from_favorite for recipe in cursor]
    return render_template('profile.html', session=session, profile=profile, recipes=recipes, faved=faved)


@app.route('/edit-profile/<profile_id>', methods=['GET', 'POST'])
def edit_profile(profile_id):
    if request.method == 'GET':
        profile = mongo.db.users.find_one({'_id': ObjectId(profile_id)})
        return render_template('editprofile.html', session=session, profile=profile)
    elif request.method == 'POST':
        data = request.form.to_dict()

        data['username'] = data['username'].title()
        data['email'] = data['email'].lower()

        update = {k: v for (k, v) in data.items() if data[k] != ""}

        # consider bio removed if not in update (empty)
        if 'bio' in update:
            update['bio'] = update['bio'].strip()
        if 'bio' not in update:
            mongo.db.users.update_one(
                {'_id': ObjectId(session['user']['_id'])},
                {'$unset': {'bio': ""}}
            )

        if 'password' in update:
            if 'passwConfirm' not in update or update['passwConfirm'] != update['password']:
                flash('Password confirmation does not match', 'warning')
                return redirect('/edit-profile/{}'.format(profile_id))
            else:
                update['password'] = hash_password(update['password'])
                del update['passwConfirm']

        # Rename and store image using user's id
        pic = request.files['img']
        if pic:
            file_ext = pic.filename.rsplit('.', 1)[-1].lower()
            filename = str(session['user']['_id']) + '.' + file_ext
            # double check file extension
            if not filename.endswith(pic_extensions):
                flash('wrong file extension', 'warning')
                return redirect('/edit-profile/{}'.format(profile_id))
            else:
                pic.save(path.join(app.config['USER_PIC_DIR'], filename))

            update['avatar'] = filename

        mongo.db.users.update_one(
            {'_id': ObjectId(session['user']['_id'])},
            {'$set': update}
        )

        # update session user object
        user = mongo.db.users.find_one({'_id': ObjectId(session['user']['_id'])}, {'username': 1, 'favorites': 1})
        user = JSONEncoder().encode(user)
        session['user'] = json.loads(user)
        session.modified = True
        return redirect('/profile/{}'.format(profile_id))


@app.route('/deluser/<user_id>')
def delete_user(user_id):
    """ Deleting the user's accounts:
    decrement fav_count for recipe in his favorite list,
    removes his recipes from other users favorite list then remove them,
    finally remove his account and logout from session.
    """

    user = mongo.db.users.find_one({'_id': ObjectId(user_id)})

    if 'recipes' in user and len(user['recipes']) > 0:
        # removes each user's recipe from other users favorites
        # removes recipe in DB and image file on server
        for recipe in user['recipes']:
            mongo.db.users.update_many({}, {'$pull': {'favorites': recipe}})
            os_remove(path.join(app.config['RECIPE_PIC_DIR'],
                                mongo.db.recipes.find_one({'_id': ObjectId(recipe)})['image']))
            mongo.db.recipes.remove({"_id": recipe})

    if 'favorites' in user and len(user['favorites']) > 0:
        # for each faved recipe decrement favorite count on recipe
        for recipe in user['favorites']:
            mongo.db.recipes.update_many({'_id': ObjectId(recipe)}, {'$inc': {'favorite': -1}})

    # then delete user and avatar file
    if user['avatar'] != "default.png":
        os_remove(path.join(app.config['USER_PIC_DIR'], user['avatar']))
    mongo.db.users.remove({"_id": ObjectId(user_id)})

    flash("We are sorry to see you leave {}. Feel free to come back anytime !".
          format(session['user']['username']), "info")
    session.pop('user')
    return redirect('/home')


@app.errorhandler(500)
@app.errorhandler(404)
def page_error(error):
    return render_template('error.html')
