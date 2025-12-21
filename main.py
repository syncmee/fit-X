from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime
import requests, ast, gunicorn
import psycopg2
import os

# Initialize the Flask app
app = Flask(__name__)
app.secret_key = 'Secret_Key'

# SQLite configuration
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///userdata.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize the database
db = SQLAlchemy(app)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password = db.Column(db.String(255), nullable=False)
    onboarding = db.Column(db.Boolean, default=False)

    # Onboarding Fields
    gender = db.Column(db.String(20))
    age = db.Column(db.Integer)
    height = db.Column(db.Float)
    weight = db.Column(db.Float)
    start_weight = db.Column(db.Float)  # ADDED: To track progress from start
    target_weight = db.Column(db.Float)  # ADDED: To store the goal
    activity_level = db.Column(db.String(50))
    diet = db.Column(db.String(50))
    goal = db.Column(db.String(50))

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)

    def __repr__(self):
        return f"<User {self.name}>"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.route('/')
def homepage():
    return render_template('homepage.html')


@app.route('/test')
def test():
    return render_template('test.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if 'email-signup' in request.form:
            name = request.form['name-signup']
            email = request.form['email-signup']
            password = request.form['password-signup']

            existing_user = User.query.filter((User.email == email) | (User.name == name)).first()
            if existing_user:
                flash('Email or Username already exists!', 'error')
                return redirect(url_for('login'))

            new_user = User(name=name, email=email, onboarding=False)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash('Sign-up successful! Please log in.', 'success')
            return redirect(url_for('login'))

        elif 'email' in request.form:
            email = request.form['email']
            password = request.form['password']

            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                login_user(user)
                if not user.onboarding:
                    return redirect(url_for('onboarding'))
                return redirect(url_for('dashboard', user=current_user.name))
            else:
                flash('Invalid email or password', 'error')
                return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/dashboard/<user>')
@login_required
def dashboard(user):
    daily_calories = 2000
    bmi = 0
    progress_percentage = 0  # Default value
    current_hour = datetime.now().hour
    greeting = "Welcome"
    meal_suggestion = "Meal"

    if 5 <= current_hour < 11:
        greeting = "Good Morning"
        meal_suggestion = "Breakfast"
    elif 11 <= current_hour < 16:
        greeting = "Good Afternoon"
        meal_suggestion = "Lunch"
    elif 16 <= current_hour < 19:
        greeting = "Good Evening"
        meal_suggestion = "Snacks"
    else:
        greeting = "Good Night"
        meal_suggestion = "Dinner"

    if current_user.weight and current_user.height and current_user.age:
        # 1. Calculate BMI
        height_in_meters = current_user.height / 100
        bmi = round(current_user.weight / (height_in_meters ** 2), 1)

        # 2. Calculate BMR
        val_weight = 10 * current_user.weight
        val_height = 6.25 * current_user.height
        val_age = 5 * current_user.age

        if current_user.gender == 'male':
            bmr = val_weight + val_height - val_age + 5
        else:
            bmr = val_weight + val_height - val_age - 161

        # 3. Calculate TDEE
        multipliers = {
            'sedentary': 1.2, 'light': 1.375, 'moderate': 1.55, 'athlete': 1.725
        }
        activity_factor = multipliers.get(current_user.activity_level, 1.2)
        tdee = bmr * activity_factor

        # 4. Adjust for Goal
        if current_user.goal == 'lose':
            daily_calories = int(tdee - 500)
        elif current_user.goal == 'gain':
            daily_calories = int(tdee + 500)
        else:
            daily_calories = int(tdee)

        # 5. Calculate Progress Percentage
        # Logic: (Start - Current) / (Start - Target)
        if current_user.start_weight and current_user.target_weight:
            try:
                total_change_needed = abs(current_user.start_weight - current_user.target_weight)
                change_achieved = abs(current_user.start_weight - current_user.weight)

                if total_change_needed > 0:
                    progress_percentage = int((change_achieved / total_change_needed) * 100)
                    # Cap at 100% or 0%
                    progress_percentage = max(0, min(100, progress_percentage))
                else:
                    progress_percentage = 100  # Target reached or start == target
            except:
                progress_percentage = 0

    weight_trends = [current_user.weight, current_user.weight, current_user.weight]

    return render_template('dashboard.html',
                           user=current_user,
                           calories=daily_calories,
                           bmi=bmi,
                           trends=weight_trends,
                           progress=progress_percentage,
                           greeting=greeting,
                           meal=meal_suggestion
                           )


@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    if request.method == 'POST':
        current_user.gender = request.form.get('gender')
        current_user.age = int(request.form.get('age'))
        current_user.height = float(request.form.get('height'))

        # Capture weight and target weight
        weight_input = float(request.form.get('weight'))
        current_user.weight = weight_input
        current_user.start_weight = weight_input  # Set start weight to initial weight
        current_user.target_weight = float(request.form.get('target_weight'))

        current_user.activity_level = request.form.get('activity-level')
        current_user.diet = request.form.get('diet')
        current_user.goal = request.form.get('goal')

        current_user.onboarding = True
        db.session.commit()
        return redirect(url_for('dashboard', user=current_user.name))

    return render_template('onboarding.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)