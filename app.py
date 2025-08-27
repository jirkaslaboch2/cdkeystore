# app.py - Flask backend for CD Key Store

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import os
import csv
import stripe
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY') # Change this to a random secret key
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'  # For uploading key files in admin

# Stripe configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY')

# Email configuration (use your own SMTP details, e.g., Gmail)
EMAIL_HOST = os.getenv('EMAIL_HOST')
EMAIL_PORT = int(os.getenv('EMAIL_PORT'))
EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASS = os.getenv('EMAIL_PASS')

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)

class Key(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    key_code = db.Column(db.String(50), unique=True, nullable=False)
    used = db.Column(db.Boolean, default=False)

class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    key_id = db.Column(db.Integer, db.ForeignKey('key.id'), nullable=False)
    transaction_id = db.Column(db.String(100), nullable=False)

# Create database tables
with app.app_context():
    db.create_all()

# Helper function to send email
def send_key_email(email, key_code, product_name):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = email
    msg['Subject'] = f'Your CD Key for {product_name}'
    
    body = f'Thank you for your purchase! Your CD Key is: {key_code}'
    msg.attach(MIMEText(body, 'plain'))
    
    server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
    server.starttls()
    server.login(EMAIL_USER, EMAIL_PASS)
    text = msg.as_string()
    server.sendmail(EMAIL_USER, email, text)
    server.quit()

# Routes

# Home/Store page
@app.route('/')
def index():
    products = Product.query.all()
    return render_template('index.html', products=products, stripe_pk=STRIPE_PUBLISHABLE_KEY)

# Product detail (optional, but good for store)
@app.route('/product/<int:product_id>')
def product_detail(product_id):
    product = Product.query.get_or_404(product_id)
    return render_template('product.html', product=product, stripe_pk=STRIPE_PUBLISHABLE_KEY)

# Create Stripe checkout session
@app.route('/create-checkout-session/<int:product_id>', methods=['POST'])
def create_checkout_session(product_id):
    if 'user_id' not in session:
        flash('Please login to purchase.')
        return redirect(url_for('login'))
    
    product = Product.query.get_or_404(product_id)
    if product.stock <= 0:
        flash('Out of stock!')
        return redirect(url_for('index'))
    
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[
                {
                    'price_data': {
                        'currency': 'usd',
                        'unit_amount': int(product.price * 100),
                        'product_data': {
                            'name': product.name,
                        },
                    },
                    'quantity': 1,
                },
            ],
            mode='payment',
            success_url=url_for('success', product_id=product_id, _external=True),
            cancel_url=url_for('index', _external=True),
        )
        return jsonify({'id': checkout_session.id})
    except Exception as e:
        return jsonify(error=str(e)), 403

# Success page after payment
@app.route('/success/<int:product_id>')
def success(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    product = Product.query.get_or_404(product_id)
    user = User.query.get(session['user_id'])
    
    # Assign a key
    available_key = Key.query.filter_by(product_id=product_id, used=False).first()
    if not available_key:
        flash('Error: No key available. Contact support.')
        return redirect(url_for('index'))
    
    available_key.used = True
    product.stock -= 1
    
    purchase = Purchase(user_id=user.id, product_id=product.id, key_id=available_key.id, transaction_id=str(uuid.uuid4()))
    db.session.add(purchase)
    db.session.commit()
    
    # Send email
    send_key_email(user.email, available_key.key_code, product.name)
    
    session['purchased_key'] = available_key.key_code  # Store temporarily for popup
    
    return render_template('success.html', product=product)

# Get key API for popup
@app.route('/get_key')
def get_key():
    if 'purchased_key' in session:
        key = session['purchased_key']
        del session['purchased_key']  # Remove after retrieval
        return jsonify({'key': key})
    return jsonify({'error': 'No key available'}), 404

# User registration
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = generate_password_hash(request.form['password'])
        
        if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first():
            flash('User already exists.')
            return redirect(url_for('register'))
        
        new_user = User(username=username, email=email, password=password)
        db.session.add(new_user)
        db.session.commit()
        flash('Registered successfully. Please login.')
        return redirect(url_for('login'))
    return render_template('register.html')

# User login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            session['is_admin'] = user.is_admin
            return redirect(url_for('index'))
        flash('Invalid credentials.')
    return render_template('login.html')

# Logout
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# Admin panel - Dashboard
@app.route('/admin')
def admin_dashboard():
    if 'user_id' not in session or not session.get('is_admin'):
        flash('Access denied.')
        return redirect(url_for('index'))
    
    products = Product.query.all()
    users = User.query.all()
    purchases = Purchase.query.all()
    return render_template('admin/dashboard.html', products=products, users=users, purchases=purchases)

# Admin - Add product
@app.route('/admin/add_product', methods=['GET', 'POST'])
def add_product():
    if 'user_id' not in session or not session.get('is_admin'):
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        name = request.form['name']
        description = request.form['description']
        price = float(request.form['price'])
        new_product = Product(name=name, description=description, price=price, stock=0)
        db.session.add(new_product)
        db.session.commit()
        flash('Product added.')
        return redirect(url_for('admin_dashboard'))
    return render_template('admin/add_product.html')

# Admin - Upload keys for product
@app.route('/admin/upload_keys/<int:product_id>', methods=['GET', 'POST'])
def upload_keys(product_id):
    if 'user_id' not in session or not session.get('is_admin'):
        return redirect(url_for('index'))
    
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        if file and file.filename.endswith('.csv'):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
            with open(file_path, 'r') as csvfile:
                reader = csv.reader(csvfile)
                for row in reader:
                    if row:
                        key_code = row[0].strip()
                        if not Key.query.filter_by(key_code=key_code).first():
                            new_key = Key(product_id=product.id, key_code=key_code)
                            db.session.add(new_key)
                            product.stock += 1
            db.session.commit()
            os.remove(file_path)  # Clean up
            flash('Keys uploaded and stock updated.')
            return redirect(url_for('admin_dashboard'))
    return render_template('admin/upload_keys.html', product=product)

# Admin - Edit product
@app.route('/admin/edit_product/<int:product_id>', methods=['GET', 'POST'])
def edit_product(product_id):
    if 'user_id' not in session or not session.get('is_admin'):
        return redirect(url_for('index'))
    
    product = Product.query.get_or_404(product_id)
    
    if request.method == 'POST':
        product.name = request.form['name']
        product.description = request.form['description']
        product.price = float(request.form['price'])
        db.session.commit()
        flash('Product updated.')
        return redirect(url_for('admin_dashboard'))
    return render_template('admin/edit_product.html', product=product)

# Admin - Delete product
@app.route('/admin/delete_product/<int:product_id>')
def delete_product(product_id):
    if 'user_id' not in session or not session.get('is_admin'):
        return redirect(url_for('index'))
    
    product = Product.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    flash('Product deleted.')
    return redirect(url_for('admin_dashboard'))

# Admin - Manage users (e.g., make admin)
@app.route('/admin/make_admin/<int:user_id>')
def make_admin(user_id):
    if 'user_id' not in session or not session.get('is_admin'):
        return redirect(url_for('index'))
    
    user = User.query.get_or_404(user_id)
    user.is_admin = True
    db.session.commit()
    flash('User promoted to admin.')
    return redirect(url_for('admin_dashboard'))

# Run the app
if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(debug=True)
