from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
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
login_manager.login_view = 'login'  # Redirect here if a user tries to access a login-required page

# Define a simple model (e.g., User model)
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password = db.Column(db.String(255), nullable=False)
    onboarding = db.Column(db.Boolean, default=False)

    # Onboarding Fields
    gender = db.Column(db.String(20))
    age = db.Column(db.Integer)
    height = db.Column(db.Float)  # Added Height
    weight = db.Column(db.Float)
    activity_level = db.Column(db.String(50))
    diet = db.Column(db.String(50))
    goal = db.Column(db.String(50))

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)


    def __repr__(self):
        return f"<User {self.name}>"

# Load user callback for Flask-Login
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Define the homepage route
@app.route('/')
def homepage():
    return render_template('homepage.html')

@app.route('/test')
def test():
    return render_template('test.html')

# Define the login route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Determine if it's a sign-in or sign-up attempt
        if 'email-signup' in request.form:
            # Sign-up logic
            name = request.form['name-signup']
            email = request.form['email-signup']
            password = request.form['password-signup']

            # Check if the email or name is already taken
            existing_user = User.query.filter((User.email == email) | (User.name == name)).first()
            if existing_user:
                flash('Email or Username already exists!', 'error')
                return redirect(url_for('login'))

            # Create a new user with onboarding set to False by default
            new_user = User(name=name, email=email, onboarding=False)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash('Sign-up successful! Please log in.', 'success')
            return redirect(url_for('login'))

        elif 'email' in request.form:
            # Sign-in logic
            email = request.form['email']
            password = request.form['password']

            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                # User is authenticated, log them in
                login_user(user)

                # Check if onboarding is complete
                if not user.onboarding:
                    return redirect(url_for('onboarding'))
                return redirect(url_for('dashboard', user=current_user.name))
            else:
                flash('Invalid email or password', 'error')
                return redirect(url_for('login'))

    # If the request method is GET, just show the login/sign-up page
    return render_template('login.html')


@app.route('/dashboard/<user>')
@login_required
def dashboard(user):
    # Default values to prevent errors if data is missing
    daily_calories = 2000
    bmi = 0
    bmr = 0

    if current_user.weight and current_user.height and current_user.age:
        # --- 1. Calculate BMI ---
        # Formula: weight (kg) / [height (m)]^2
        height_in_meters = current_user.height / 100
        bmi = round(current_user.weight / (height_in_meters ** 2), 1)

        # --- 2. Calculate BMR (Mifflin-St Jeor Equation) ---
        # Male: 10W + 6.25H - 5A + 5
        # Female: 10W + 6.25H - 5A - 161
        val_weight = 10 * current_user.weight
        val_height = 6.25 * current_user.height
        val_age = 5 * current_user.age

        if current_user.gender == 'male':
            bmr = val_weight + val_height - val_age + 5
        else:
            bmr = val_weight + val_height - val_age - 161

        # --- 3. Calculate TDEE (Daily Calories) ---
        multipliers = {
            'sedentary': 1.2,
            'light': 1.375,
            'moderate': 1.55,
            'athlete': 1.725  # mapped from 'high performance'
        }
        # Default to 1.2 if not found
        activity_factor = multipliers.get(current_user.activity_level, 1.2)
        tdee = bmr * activity_factor

        # --- 4. Adjust for Goal ---
        if current_user.goal == 'lose':
            daily_calories = int(tdee - 500)
        elif current_user.goal == 'gain':
            daily_calories = int(tdee + 500)
        else:
            daily_calories = int(tdee)

    # Sample chart data (placeholder for now)
    weight_trends = [current_user.weight, current_user.weight, current_user.weight]

    return render_template('dashboard.html',
                           user=current_user,
                           calories=daily_calories,
                           bmi=bmi,
                           trends=weight_trends)


@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    if request.method == 'POST':
        current_user.gender = request.form.get('gender')
        current_user.age = int(request.form.get('age'))
        current_user.height = float(request.form.get('height'))  # Save Height
        current_user.weight = float(request.form.get('weight'))
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

# Run the Flask app
if __name__ == '__main__':
    # Create the database and tables if they don't exist
    with app.app_context():
        db.create_all()  # This will create the SQLite database file (site.db) and tables
    app.run(debug=True)
