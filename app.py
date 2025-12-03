from flask import Flask, render_template, request, redirect, url_for
import pymysql
from datetime import datetime

app = Flask(__name__)

# Jinja2 filter for date formatting
@app.template_filter('dateformat')
def dateformat(value, format='%Y-%m-%d'):
    """Format a date value"""
    if value is None:
        return 'N/A'
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        except:
            try:
                value = datetime.strptime(value, '%Y-%m-%d')
            except:
                return value
    if isinstance(value, datetime):
        return value.strftime(format)
    return str(value)

# -----------------------------
# Database connection helper
# -----------------------------
def get_connection():
    return pymysql.connect(
        host="localhost",
        port=3307,
        user="root",
        password="Qh#330320",
        database="UniLibPlus",
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4"
    )

# -----------------------------
# Dashboard - Home page with statistics
# -----------------------------
@app.route("/")
@app.route("/dashboard")
def dashboard():
    conn = get_connection()
    stats = {}
    try:
        with conn.cursor() as cur:
            # -------------------------------------------------
            # Use updated_complex_query.sql "DASHBOARD MEGA SUMMARY"
            # -------------------------------------------------
            cur.execute("""
                SET @today := CURDATE();
            """)
            cur.execute("""
                SET @last_7 := DATE_SUB(@today, INTERVAL 7 DAY);
            """)
            cur.execute("""
                SET @last_90 := DATE_SUB(@today, INTERVAL 90 DAY);
            """)
            cur.execute("""
                WITH
                BookStats AS (
                  SELECT
                    COUNT(*) AS total_books,
                    COUNT(DISTINCT c.copy_id) AS total_copies,
                    COUNT(DISTINCT bs.subject_id) AS total_subjects
                  FROM Book b
                  LEFT JOIN Copy c       ON b.isbn = c.isbn
                  LEFT JOIN BookSubject bs ON b.isbn = bs.isbn
                ),
                PatronStats AS (
                  SELECT
                    COUNT(*) AS total_patrons,
                    COUNT(DISTINCT l.patron_id) AS active_patrons_90d
                  FROM Patron p
                  LEFT JOIN Loan l
                    ON p.patron_id = l.patron_id
                   AND l.loan_ts >= @last_90
                ),
                LoanCore AS (
                  SELECT
                    l.loan_id,
                    l.loan_ts,
                    l.due_ts,
                    l.return_ts,
                    DATEDIFF(COALESCE(l.return_ts, @today), DATE(l.loan_ts)) AS duration_days
                  FROM Loan l
                ),
                LoanStats AS (
                  SELECT
                    COUNT(*) AS total_loans,
                    SUM(CASE WHEN return_ts IS NULL THEN 1 ELSE 0 END) AS current_loans,
                    SUM(
                      CASE
                        WHEN return_ts IS NULL AND due_ts < @today THEN 1
                        ELSE 0
                      END
                    ) AS overdue_loans,
                    AVG(duration_days) AS avg_duration_days
                  FROM LoanCore
                ),
                RecentLoanActivity AS (
                  SELECT
                    SUM(CASE WHEN DATE(loan_ts)   >= @last_7 THEN 1 ELSE 0 END) AS loans_last_7d,
                    SUM(CASE WHEN DATE(return_ts) >= @last_7 THEN 1 ELSE 0 END) AS returns_last_7d
                  FROM LoanCore
                )
                SELECT
                  bs.total_books,
                  bs.total_copies,
                  bs.total_subjects,
                  ps.total_patrons,
                  ps.active_patrons_90d,
                  ls.total_loans,
                  ls.current_loans,
                  ls.overdue_loans,
                  ROUND(ls.avg_duration_days, 2) AS avg_duration_days,
                  ra.loans_last_7d,
                  ra.returns_last_7d
                FROM BookStats bs
                CROSS JOIN PatronStats ps
                CROSS JOIN LoanStats   ls
                CROSS JOIN RecentLoanActivity ra;
            """)
            summary = cur.fetchone() or {}
            stats.update(summary)

            # For compatibility with existing template keys
            stats['total_books'] = stats.get('total_books', 0)
            stats['total_patrons'] = stats.get('total_patrons', 0)
            stats['current_loans'] = stats.get('current_loans', 0)
            stats['overdue_loans'] = stats.get('overdue_loans', 0)

            # -------------------------------------------------
            # Use updated_complex_query.sql "DASHBOARD OVERDUE ALERT WITH RISK SCORE"
            # -------------------------------------------------
            cur.execute("SET @today := CURDATE();")
            cur.execute("""
                WITH PatronHistory AS (
                  SELECT
                    p.patron_id,
                    COUNT(l.loan_id) AS all_loans,
                    SUM(
                      CASE
                        WHEN l.return_ts IS NULL AND l.due_ts < @today THEN 1
                        WHEN l.return_ts IS NOT NULL AND l.return_ts > l.due_ts THEN 1
                        ELSE 0
                      END
                    ) AS overdue_or_late_loans
                  FROM Patron p
                  LEFT JOIN Loan l ON p.patron_id = l.patron_id
                  GROUP BY p.patron_id
                ),
                PatronFineSummary AS (
                  SELECT
                    patron_id,
                    COALESCE(SUM(amount), 0) AS total_fines,
                    COALESCE(SUM(CASE WHEN status = 'Unpaid' THEN amount ELSE 0 END), 0) AS unpaid_fines
                  FROM Fine
                  GROUP BY patron_id
                ),
                OverdueLoans AS (
                  SELECT
                    l.loan_id,
                    l.patron_id,
                    l.copy_id,
                    l.loan_ts,
                    l.due_ts,
                    l.return_ts,
                    DATEDIFF(@today, DATE(l.due_ts)) AS days_overdue
                  FROM Loan l
                  WHERE l.return_ts IS NULL
                    AND l.due_ts < @today
                )
                SELECT
                  o.loan_id,
                  o.patron_id,
                  CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
                  b.title AS book_title,
                  DATE(o.due_ts) AS due_date,
                  o.days_overdue,
                  ph.all_loans,
                  ph.overdue_or_late_loans,
                  pfs.unpaid_fines,
                  (
                    o.days_overdue * 1.0
                    + ph.overdue_or_late_loans * 2.0
                    + (pfs.unpaid_fines / 10.0)
                  ) AS risk_score
                FROM OverdueLoans o
                JOIN Patron p ON o.patron_id = p.patron_id
                JOIN Copy   c ON o.copy_id   = c.copy_id
                JOIN Book   b ON c.isbn      = b.isbn
                LEFT JOIN PatronHistory    ph  ON p.patron_id = ph.patron_id
                LEFT JOIN PatronFineSummary pfs ON p.patron_id = pfs.patron_id
                ORDER BY risk_score DESC, o.days_overdue DESC
                LIMIT 10;
            """)
            stats['overdue_risk'] = cur.fetchall()

            # Also compute total unpaid fines for display
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS total_unpaid
                FROM Fine
                WHERE status = 'Unpaid'
            """)
            row = cur.fetchone()
            stats['total_fines'] = row['total_unpaid'] if row else 0
    finally:
        conn.close()

    return render_template("dashboard.html", stats=stats)

# -----------------------------
# Books - Enhanced with search and filters
# -----------------------------
@app.route("/books")
def books():
    search = request.args.get('search', '')
    subject = request.args.get('subject', '')
    
    # Normalize filters
    subject_id = int(subject) if subject and subject.isdigit() else None
    search_term = search.strip() or None

    conn = get_connection()
    books = []
    subjects = []
    
    try:
        with conn.cursor() as cur:
            # Get all subjects for filter
            cur.execute("SELECT DISTINCT subject_id, name FROM Subject ORDER BY name")
            subjects = cur.fetchall()
            
            # Use updated_complex_query.sql "BOOKS CATALOG – ADVANCED SEARCH + SUBJECT RANK"
            sql = """
                WITH BookCore AS (
                  SELECT
                    b.isbn,
                    b.title,
                    b.pub_year,
                    b.publisher_id,
                    MIN(bs.subject_id) AS primary_subject_id
                  FROM Book b
                  LEFT JOIN BookSubject bs ON b.isbn = bs.isbn
                  GROUP BY b.isbn, b.title, b.pub_year, b.publisher_id
                ),
                BookPopularity AS (
                  SELECT
                    bc.isbn,
                    bc.primary_subject_id,
                    COUNT(l.loan_id) AS times_loaned
                  FROM BookCore bc
                  LEFT JOIN Copy c ON bc.isbn = c.isbn
                  LEFT JOIN Loan l ON c.copy_id = l.copy_id
                  GROUP BY bc.isbn, bc.primary_subject_id
                ),
                BookAvailability AS (
                  SELECT
                    bc.isbn,
                    COUNT(DISTINCT c.copy_id) AS total_copies,
                    SUM(
                      CASE
                        WHEN l.loan_id IS NULL OR l.return_ts IS NOT NULL THEN 1
                        ELSE 0
                      END
                    ) AS available_copies
                  FROM BookCore bc
                  LEFT JOIN Copy c ON bc.isbn = c.isbn
                  LEFT JOIN Loan l ON c.copy_id = l.copy_id
                                    AND l.return_ts IS NULL
                  GROUP BY bc.isbn
                ),
                BookDetail AS (
                  SELECT
                    bc.isbn,
                    bc.title,
                    bc.pub_year,
                    bc.publisher_id,
                    bc.primary_subject_id,
                    GROUP_CONCAT(DISTINCT CONCAT(a.first_name, ' ', a.last_name)
                                 ORDER BY a.last_name SEPARATOR ', ') AS authors,
                    GROUP_CONCAT(DISTINCT s.name
                                 ORDER BY s.name SEPARATOR ', ') AS subjects
                  FROM BookCore bc
                  LEFT JOIN BookAuthor ba   ON bc.isbn = ba.isbn
                  LEFT JOIN Author a        ON ba.author_id = a.author_id
                  LEFT JOIN BookSubject bs2 ON bc.isbn = bs2.isbn
                  LEFT JOIN Subject s       ON bs2.subject_id = s.subject_id
                  GROUP BY bc.isbn, bc.title, bc.pub_year, bc.publisher_id, bc.primary_subject_id
                )
                SELECT
                  bd.isbn,
                  bd.title,
                  bd.pub_year,
                  pub.name AS publisher_name,
                  bd.authors,
                  bd.subjects,
                  COALESCE(bp.times_loaned, 0) AS times_loaned,
                  ba.total_copies,
                  ba.available_copies,
                  s_main.name AS primary_subject,
                  RANK() OVER (
                    PARTITION BY bd.primary_subject_id
                    ORDER BY COALESCE(bp.times_loaned, 0) DESC, bd.title
                  ) AS subject_popularity_rank
                FROM BookDetail bd
                LEFT JOIN Publisher pub ON bd.publisher_id     = pub.publisher_id
                LEFT JOIN BookPopularity  bp ON bd.isbn        = bp.isbn
                LEFT JOIN BookAvailability ba ON bd.isbn       = ba.isbn
                LEFT JOIN Subject s_main  ON bd.primary_subject_id = s_main.subject_id
                WHERE
                  (%s IS NULL OR %s = ''
                   OR bd.title LIKE CONCAT('%%', %s, '%%')
                   OR bd.isbn  LIKE CONCAT('%%', %s, '%%')
                  )
                  AND (
                    %s IS NULL
                    OR bd.primary_subject_id = %s
                  )
                ORDER BY
                  COALESCE(bp.times_loaned, 0) DESC,
                  bd.title;
            """
            # parameters: search_term x4, subject_id x2 (MySQL will handle NULL)
            params = [
                search_term, search_term or '',
                search_term or '', search_term or '',
                subject_id, subject_id
            ]
            cur.execute(sql, params)
            books = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("books.html", books=books, subjects=subjects, search=search, selected_subject=subject)

# -----------------------------
# Book Details
# -----------------------------
@app.route("/book/<isbn>")
def book_detail(isbn):
    conn = get_connection()
    book = None
    copies = []
    
    try:
        with conn.cursor() as cur:
            # Get book details
            cur.execute("""
                SELECT 
                    b.isbn,
                    b.title,
                    b.pub_year,
                    p.name AS publisher_name,
                    GROUP_CONCAT(DISTINCT CONCAT(a.first_name, ' ', a.last_name) SEPARATOR ', ') AS authors,
                    GROUP_CONCAT(DISTINCT s.name SEPARATOR ', ') AS subjects
                FROM Book b
                LEFT JOIN Publisher p ON b.publisher_id = p.publisher_id
                LEFT JOIN BookAuthor ba ON b.isbn = ba.isbn
                LEFT JOIN Author a ON ba.author_id = a.author_id
                LEFT JOIN BookSubject bs ON b.isbn = bs.isbn
                LEFT JOIN Subject s ON bs.subject_id = s.subject_id
                WHERE b.isbn = %s
                GROUP BY b.isbn, b.title, b.pub_year, p.name
            """, (isbn,))
            book = cur.fetchone()
            
            # Get copy locations
            cur.execute("""
                SELECT copy_id, barcode, branch_name, checkin_term
                FROM vw_copy_location
                WHERE isbn = %s
            """, (isbn,))
            copies = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("book_detail.html", book=book, copies=copies)

# -----------------------------
# Patrons - Enhanced
# -----------------------------
@app.route("/patrons", methods=["GET", "POST"])
def patrons():
    if request.method == "POST":
        first_name = request.form.get("first_name")
        last_name = request.form.get("last_name")
        email = request.form.get("email")
        patron_type = request.form.get("patron_type")
        address_id = request.form.get("address_id")

        if first_name and last_name and patron_type:
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    sql = """
                        INSERT INTO Patron (patron_id, first_name, last_name, email, patron_type, address_id, balance)
                        VALUES (
                            (SELECT COALESCE(MAX(patron_id), 0) + 1 FROM Patron p2),
                            %s, %s, %s, %s, %s, 0.00
                        )
                    """
                    cur.execute(sql, (first_name, last_name, email, patron_type, address_id or None))
                conn.commit()
            finally:
                conn.close()

        return redirect(url_for("patrons"))

    # GET: show list of patrons with activity & risk profile
    conn = get_connection()
    patrons = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH LoanAgg AS (
                  SELECT
                    p.patron_id,
                    COUNT(l.loan_id) AS total_loans,
                    SUM(CASE WHEN l.return_ts IS NULL THEN 1 ELSE 0 END) AS active_loans,
                    SUM(CASE WHEN l.return_ts IS NULL AND l.due_ts < CURDATE() THEN 1 ELSE 0 END)
                      AS overdue_loans,
                    MAX(l.loan_ts) AS last_loan_ts
                  FROM Patron p
                  LEFT JOIN Loan l ON p.patron_id = l.patron_id
                  GROUP BY p.patron_id
                ),
                FineAgg AS (
                  SELECT
                    patron_id,
                    COALESCE(SUM(amount), 0) AS total_fines,
                    COALESCE(SUM(CASE WHEN status = 'Unpaid' THEN amount ELSE 0 END), 0) AS unpaid_fines
                  FROM Fine
                  GROUP BY patron_id
                )
                SELECT
                  p.patron_id,
                  p.first_name,
                  p.last_name,
                  p.email,
                  p.patron_type,
                  p.balance,
                  COALESCE(la.total_loans, 0) AS total_loans,
                  COALESCE(la.active_loans, 0) AS active_loans,
                  COALESCE(la.overdue_loans, 0) AS overdue_loans,
                  la.last_loan_ts,
                  COALESCE(fa.total_fines, 0)   AS total_fines,
                  COALESCE(fa.unpaid_fines, 0)  AS unpaid_fines,
                  CASE
                    WHEN COALESCE(fa.unpaid_fines, 0) >= 50
                       OR COALESCE(la.overdue_loans, 0) >= 3
                    THEN 'HIGH'
                    WHEN COALESCE(fa.unpaid_fines, 0) BETWEEN 10 AND 49
                       OR COALESCE(la.overdue_loans, 0) BETWEEN 1 AND 2
                    THEN 'MEDIUM'
                    ELSE 'LOW'
                  END AS risk_level
                FROM Patron p
                LEFT JOIN LoanAgg la ON p.patron_id = la.patron_id
                LEFT JOIN FineAgg fa ON p.patron_id = fa.patron_id
                ORDER BY
                  CASE risk_level
                    WHEN 'HIGH' THEN 1
                    WHEN 'MEDIUM' THEN 2
                    ELSE 3
                  END,
                  p.patron_id
            """)
            patrons = cur.fetchall()
    finally:
        conn.close()

    return render_template("patrons.html", patrons=patrons)

# -----------------------------
# Patron Details
# -----------------------------
@app.route("/patron/<int:patron_id>")
def patron_detail(patron_id):
    conn = get_connection()
    patron = None
    loans = []
    fines = None
    
    try:
        with conn.cursor() as cur:
            # Get patron info
            cur.execute("""
                SELECT p.*, ps.total_fines, ps.unpaid_fines
                FROM Patron p
                LEFT JOIN vw_patron_fines_summary ps ON p.patron_id = ps.patron_id
                WHERE p.patron_id = %s
            """, (patron_id,))
            patron = cur.fetchone()
            
            # Get loan history
            cur.execute("""
                SELECT loan_id, copy_id, barcode, isbn, title, loan_ts, due_ts, return_ts, loan_status
                FROM vw_patron_loans_with_status
                WHERE patron_id = %s
                ORDER BY loan_ts DESC
            """, (patron_id,))
            loans = cur.fetchall()
            
            # Get fines summary
            cur.execute("""
                SELECT total_fines, unpaid_fines
                FROM vw_patron_fines_summary
                WHERE patron_id = %s
            """, (patron_id,))
            fines = cur.fetchone()
    finally:
        conn.close()
    
    return render_template("patron_detail.html", patron=patron, loans=loans, fines=fines)

# -----------------------------
# Loans Management
# -----------------------------
@app.route("/loans")
def loans():
    filter_type = request.args.get('filter', 'all')  # all, current, overdue, returned
    
    conn = get_connection()
    loans = []
    
    try:
        with conn.cursor() as cur:
            # Map filter_type to status_filter used in updated_complex_query.sql
            status_filter = 'ALL'
            if filter_type == 'current':
                status_filter = 'CURRENT'
            elif filter_type == 'overdue':
                status_filter = 'OVERDUE'
            elif filter_type == 'returned':
                status_filter = 'RETURNED'

            sql = """
                WITH LoanEnriched AS (
                  SELECT
                    l.loan_id,
                    l.copy_id,
                    l.patron_id,
                    c.barcode,
                    b.isbn,
                    b.title,
                    br.branch_id,
                    br.name AS branch_name,
                    l.loan_ts,
                    l.due_ts,
                    l.return_ts,
                    CASE
                      WHEN l.return_ts IS NOT NULL THEN 'RETURNED'
                      WHEN l.due_ts < CURDATE()   THEN 'OVERDUE'
                      ELSE 'CURRENT'
                    END AS status,
                    DATEDIFF(COALESCE(l.return_ts, CURDATE()), DATE(l.loan_ts)) AS duration_days
                  FROM Loan l
                  JOIN Copy  c ON l.copy_id   = c.copy_id
                  JOIN Book  b ON c.isbn      = b.isbn
                  JOIN Branch br ON c.branch_id = br.branch_id
                ),
                PatronLoanStats AS (
                  SELECT
                    patron_id,
                    AVG(duration_days) AS avg_duration_per_patron
                  FROM LoanEnriched
                  GROUP BY patron_id
                )
                SELECT
                  le.loan_id,
                  le.patron_id,
                  CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
                  le.barcode,
                  le.isbn,
                  le.title,
                  le.branch_name,
                  le.loan_ts,
                  le.due_ts,
                  le.return_ts,
                  le.status,
                  le.duration_days,
                  pls.avg_duration_per_patron,
                  CASE
                    WHEN le.duration_days > pls.avg_duration_per_patron THEN 'LONGER THAN USUAL'
                    WHEN le.duration_days < pls.avg_duration_per_patron THEN 'SHORTER THAN USUAL'
                    ELSE 'TYPICAL'
                  END AS duration_vs_usual
                FROM LoanEnriched le
                JOIN Patron p ON le.patron_id = p.patron_id
                LEFT JOIN PatronLoanStats pls ON le.patron_id = pls.patron_id
                WHERE
                  %s = 'ALL'
                  OR (%s = 'CURRENT'  AND le.status = 'CURRENT')
                  OR (%s = 'OVERDUE'  AND le.status = 'OVERDUE')
                  OR (%s = 'RETURNED' AND le.status = 'RETURNED')
                ORDER BY le.loan_ts DESC
                LIMIT 200;
            """
            params = [status_filter, status_filter, status_filter, status_filter]
            cur.execute(sql, params)
            loans = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("loans.html", loans=loans, filter_type=filter_type)

# -----------------------------
# Fines Management
# -----------------------------
@app.route("/fines")
def fines():
    conn = get_connection()
    fines_list = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT patron_id, first_name, last_name, email, total_fines, unpaid_fines
                FROM vw_patron_fines_summary
                WHERE unpaid_fines > 0
                ORDER BY unpaid_fines DESC
            """)
            fines_list = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("fines.html", fines_list=fines_list)

# -----------------------------
# Statistics
# -----------------------------
@app.route("/statistics")
@app.route("/statistics")
def statistics():
    mode = request.args.get('mode', 'overall')

    conn = get_connection()
    books = []

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:

            if mode == 'overall':
                sql = """
                SELECT
                    RANK() OVER (ORDER BY total_loans DESC, b.title) AS ranking,
                    b.title,
                    GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR ', ') AS subject,
                    total_loans,
                    total_copies,
                    checkout_rate,
                    GROUP_CONCAT(DISTINCT s.name ORDER BY s.name SEPARATOR ', ') AS subjects
                FROM (
                    SELECT
                        b.isbn,
                        b.title,
                        COUNT(l.loan_id) AS total_loans,
                        COUNT(DISTINCT c.copy_id) AS total_copies,
                        CONCAT(ROUND(100.0 * SUM(CASE WHEN l.return_ts IS NULL THEN 1 ELSE 0 END) 
                              / NULLIF(COUNT(DISTINCT c.copy_id), 0), 1), '%') AS checkout_rate
                    FROM Book b
                    LEFT JOIN Copy c ON b.isbn = c.isbn
                    LEFT JOIN Loan l ON c.copy_id = l.copy_id
                    GROUP BY b.isbn, b.title
                ) AS book_stats
                JOIN Book b ON book_stats.isbn = b.isbn
                LEFT JOIN BookSubject bs ON b.isbn = bs.isbn
                LEFT JOIN Subject s ON bs.subject_id = s.subject_id
                GROUP BY b.isbn, b.title, total_loans, total_copies, checkout_rate
                ORDER BY ranking
                LIMIT 10
                """
                cur.execute(sql)

            else:
                subject_id = int(mode) if mode.isdigit() else 1
                sql = f"""
                SELECT
                    RANK() OVER (ORDER BY COUNT(l.loan_id) DESC, b.title) AS ranking,
                    b.title,
                    s.name AS subject,
                    COUNT(l.loan_id) AS total_loans,
                    COUNT(DISTINCT c.copy_id) AS total_copies,
                    CONCAT(ROUND(100.0 * SUM(CASE WHEN l.return_ts IS NULL THEN 1 ELSE 0 END) 
                          / NULLIF(COUNT(DISTINCT c.copy_id), 0), 1), '%') AS checkout_rate,
                    s.name AS subjects
                FROM Book b
                LEFT JOIN Copy c ON b.isbn = c.isbn
                LEFT JOIN Loan l ON c.copy_id = l.copy_id
                INNER JOIN BookSubject bs ON b.isbn = bs.isbn
                INNER JOIN Subject s ON bs.subject_id = s.subject_id
                WHERE s.subject_id = {subject_id}
                GROUP BY b.isbn, b.title, s.name
                ORDER BY ranking
                LIMIT 10
                """
                cur.execute(sql)

            books = cur.fetchall()

    except Exception as e:
        print(f"Error: {e}")
        flash("Failed to load rankings", "danger")
    finally:
        conn.close()

    return render_template("statistics.html", books=books, current_mode=mode)

# -----------------------------
# Q12: Window Functions - Rank patrons by fines within each type
# -----------------------------
@app.route("/analytics/patron-ranking")
def patron_ranking():
    conn = get_connection()
    rankings = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  patron_id,
                  first_name,
                  last_name,
                  patron_type,
                  total_fines,
                  unpaid_fines,
                  ROW_NUMBER() OVER (PARTITION BY patron_type ORDER BY total_fines DESC) AS row_num,
                  RANK() OVER (PARTITION BY patron_type ORDER BY total_fines DESC) AS rank_fines,
                  DENSE_RANK() OVER (PARTITION BY patron_type ORDER BY total_fines DESC) AS dense_rank_fines
                FROM vw_patron_fines_summary
                JOIN Patron p USING (patron_id)
                WHERE total_fines > 0
                ORDER BY patron_type, total_fines DESC
            """)
            rankings = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_patron_ranking.html", rankings=rankings)

# -----------------------------
# Q14: CTE - Patrons with loans in multiple branches
# -----------------------------
@app.route("/analytics/multi-branch-patrons")
def multi_branch_patrons():
    conn = get_connection()
    patrons = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH PatronBranchLoans AS (
                  SELECT DISTINCT
                    l.patron_id,
                    c.branch_id,
                    br.name AS branch_name,
                    COUNT(DISTINCT l.loan_id) AS loans_at_branch
                  FROM Loan l
                  JOIN Copy c ON l.copy_id = c.copy_id
                  JOIN Branch br ON c.branch_id = br.branch_id
                  GROUP BY l.patron_id, c.branch_id, br.name
                ),
                MultiBranchPatrons AS (
                  SELECT
                    patron_id,
                    COUNT(DISTINCT branch_id) AS num_branches,
                    SUM(loans_at_branch) AS total_loans
                  FROM PatronBranchLoans
                  GROUP BY patron_id
                  HAVING num_branches > 1
                )
                SELECT
                  p.patron_id,
                  CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
                  mbp.num_branches,
                  mbp.total_loans,
                  GROUP_CONCAT(DISTINCT pbl.branch_name ORDER BY pbl.branch_name SEPARATOR ', ') AS branches_used
                FROM MultiBranchPatrons mbp
                JOIN Patron p ON mbp.patron_id = p.patron_id
                JOIN PatronBranchLoans pbl ON mbp.patron_id = pbl.patron_id
                GROUP BY p.patron_id, patron_name, mbp.num_branches, mbp.total_loans
                ORDER BY mbp.num_branches DESC, mbp.total_loans DESC
            """)
            patrons = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_multi_branch.html", patrons=patrons)

# -----------------------------
# Q15: Complex CASE - Book popularity categories
# -----------------------------
@app.route("/analytics/book-popularity")
def book_popularity():
    conn = get_connection()
    categories = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  CASE
                    WHEN loan_count = 0 THEN 'Never Loaned'
                    WHEN loan_count BETWEEN 1 AND 5 THEN 'Low Popularity (1-5 loans)'
                    WHEN loan_count BETWEEN 6 AND 15 THEN 'Medium Popularity (6-15 loans)'
                    WHEN loan_count BETWEEN 16 AND 30 THEN 'High Popularity (16-30 loans)'
                    ELSE 'Very High Popularity (30+ loans)'
                  END AS popularity_category,
                  COUNT(*) AS num_books,
                  AVG(loan_count) AS avg_loans_per_book,
                  MIN(loan_count) AS min_loans,
                  MAX(loan_count) AS max_loans,
                  SUM(loan_count) AS total_loans
                FROM (
                  SELECT
                    b.isbn,
                    b.title,
                    COUNT(l.loan_id) AS loan_count
                  FROM Book b
                  LEFT JOIN Copy c ON b.isbn = c.isbn
                  LEFT JOIN Loan l ON c.copy_id = l.copy_id
                  GROUP BY b.isbn, b.title
                ) AS book_loans
                GROUP BY popularity_category
                ORDER BY 
                  CASE popularity_category
                    WHEN 'Never Loaned' THEN 1
                    WHEN 'Low Popularity (1-5 loans)' THEN 2
                    WHEN 'Medium Popularity (6-15 loans)' THEN 3
                    WHEN 'High Popularity (16-30 loans)' THEN 4
                    ELSE 5
                  END
            """)
            categories = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_book_popularity.html", categories=categories)

# -----------------------------
# Q16: EXISTS - Patrons with reservations but no loans
# -----------------------------
@app.route("/analytics/reservations-no-loans")
def reservations_no_loans():
    conn = get_connection()
    patrons = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  p.patron_id,
                  CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
                  p.email,
                  p.patron_type,
                  COUNT(r.reservation_id) AS active_reservations
                FROM Patron p
                JOIN Reservation r ON p.patron_id = r.patron_id
                WHERE r.status IN ('Active', 'Waiting')
                  AND NOT EXISTS (
                    SELECT 1
                    FROM Loan l
                    WHERE l.patron_id = p.patron_id
                  )
                GROUP BY p.patron_id, patron_name, p.email, p.patron_type
                ORDER BY active_reservations DESC, patron_name
            """)
            patrons = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_reservations_no_loans.html", patrons=patrons)

# -----------------------------
# Q19: Self-Join - Repeat borrowers of same book
# -----------------------------
@app.route("/analytics/repeat-borrowers")
def repeat_borrowers():
    conn = get_connection()
    borrowers = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  p.patron_id,
                  CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
                  b.isbn,
                  b.title,
                  COUNT(DISTINCT l1.loan_id) AS times_borrowed,
                  MIN(l1.loan_ts) AS first_loan,
                  MAX(l1.loan_ts) AS last_loan,
                  DATEDIFF(MAX(l1.loan_ts), MIN(l1.loan_ts)) AS days_between_first_last
                FROM Patron p
                JOIN Loan l1 ON p.patron_id = l1.patron_id
                JOIN Copy c1 ON l1.copy_id = c1.copy_id
                JOIN Book b ON c1.isbn = b.isbn
                GROUP BY p.patron_id, patron_name, b.isbn, b.title
                HAVING times_borrowed > 1
                ORDER BY times_borrowed DESC, patron_name, b.title
            """)
            borrowers = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_repeat_borrowers.html", borrowers=borrowers)

# -----------------------------
# Q23: Fine analysis by reason
# -----------------------------
@app.route("/analytics/fine-analysis")
def fine_analysis():
    conn = get_connection()
    fine_stats = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  fr.code AS fine_reason_code,
                  fr.description AS fine_reason,
                  COUNT(f.fine_id) AS total_fines,
                  SUM(f.amount) AS total_amount,
                  AVG(f.amount) AS avg_amount,
                  MIN(f.amount) AS min_amount,
                  MAX(f.amount) AS max_amount,
                  SUM(CASE WHEN f.status = 'Paid' THEN f.amount ELSE 0 END) AS paid_amount,
                  SUM(CASE WHEN f.status = 'Unpaid' THEN f.amount ELSE 0 END) AS unpaid_amount,
                  COUNT(CASE WHEN f.status = 'Paid' THEN 1 END) AS paid_count,
                  COUNT(CASE WHEN f.status = 'Unpaid' THEN 1 END) AS unpaid_count,
                  ROUND(
                    100.0 * COUNT(CASE WHEN f.status = 'Paid' THEN 1 END) / COUNT(f.fine_id),
                    2
                  ) AS payment_rate_pct
                FROM FineReason fr
                LEFT JOIN Fine f ON fr.reason_id = f.reason_id
                GROUP BY fr.reason_id, fr.code, fr.description
                ORDER BY total_amount DESC
            """)
            fine_stats = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_fine_analysis.html", fine_stats=fine_stats)

# -----------------------------
# Q22: Patron borrowing patterns by subject
# -----------------------------
@app.route("/analytics/subject-patterns")
def subject_patterns():
    patron_id = request.args.get('patron_id', '')
    conn = get_connection()
    patterns = []
    
    try:
        with conn.cursor() as cur:
            if patron_id:
                sql = """
                    WITH PatronSubjectLoans AS (
                      SELECT
                        l.patron_id,
                        s.subject_id,
                        s.name AS subject_name,
                        COUNT(DISTINCT l.loan_id) AS loan_count,
                        COUNT(DISTINCT b.isbn) AS unique_books
                      FROM Loan l
                      JOIN Copy c ON l.copy_id = c.copy_id
                      JOIN Book b ON c.isbn = b.isbn
                      JOIN BookSubject bs ON b.isbn = bs.isbn
                      JOIN Subject s ON bs.subject_id = s.subject_id
                      WHERE l.patron_id = %s
                      GROUP BY l.patron_id, s.subject_id, s.name
                    ),
                    PatronTotalLoans AS (
                      SELECT
                        patron_id,
                        SUM(loan_count) AS total_loans
                      FROM PatronSubjectLoans
                      GROUP BY patron_id
                    )
                    SELECT
                      p.patron_id,
                      CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
                      p.patron_type,
                      psl.subject_name,
                      psl.loan_count,
                      psl.unique_books,
                      ROUND(100.0 * psl.loan_count / ptl.total_loans, 2) AS pct_of_total_loans
                    FROM PatronSubjectLoans psl
                    JOIN Patron p ON psl.patron_id = p.patron_id
                    JOIN PatronTotalLoans ptl ON p.patron_id = ptl.patron_id
                    WHERE ptl.total_loans >= 5
                    ORDER BY psl.loan_count DESC
                """
                cur.execute(sql, (patron_id,))
            else:
                sql = """
                    WITH PatronSubjectLoans AS (
                      SELECT
                        l.patron_id,
                        s.subject_id,
                        s.name AS subject_name,
                        COUNT(DISTINCT l.loan_id) AS loan_count,
                        COUNT(DISTINCT b.isbn) AS unique_books
                      FROM Loan l
                      JOIN Copy c ON l.copy_id = c.copy_id
                      JOIN Book b ON c.isbn = b.isbn
                      JOIN BookSubject bs ON b.isbn = bs.isbn
                      JOIN Subject s ON bs.subject_id = s.subject_id
                      GROUP BY l.patron_id, s.subject_id, s.name
                    ),
                    PatronTotalLoans AS (
                      SELECT
                        patron_id,
                        SUM(loan_count) AS total_loans
                      FROM PatronSubjectLoans
                      GROUP BY patron_id
                    )
                    SELECT
                      p.patron_id,
                      CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
                      p.patron_type,
                      psl.subject_name,
                      psl.loan_count,
                      psl.unique_books,
                      ROUND(100.0 * psl.loan_count / ptl.total_loans, 2) AS pct_of_total_loans
                    FROM PatronSubjectLoans psl
                    JOIN Patron p ON psl.patron_id = p.patron_id
                    JOIN PatronTotalLoans ptl ON p.patron_id = ptl.patron_id
                    WHERE ptl.total_loans >= 5
                    ORDER BY p.patron_id, psl.loan_count DESC
                    LIMIT 100
                """
                cur.execute(sql)
            
            patterns = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_subject_patterns.html", patterns=patterns, patron_id=patron_id)

# -----------------------------
# Q25: Monthly loans by patron type (PIVOT-like)
# -----------------------------
@app.route("/analytics/monthly-loans")
def monthly_loans():
    conn = get_connection()
    monthly_data = []
    
    try:
        with conn.cursor() as cur:
            # Use updated_complex_query.sql "STATISTICS – MONTHLY LOAN TRENDS BY PATRON TYPE"
            cur.execute("""
                WITH MonthlyTypeLoans AS (
                  SELECT
                    DATE_FORMAT(l.loan_ts, '%%Y-%%m') AS loan_month,
                    p.patron_type
                  FROM Loan l
                  JOIN Patron p ON l.patron_id = p.patron_id
                  WHERE l.loan_ts IS NOT NULL
                ),
                MonthlyAgg AS (
                  SELECT
                    loan_month,
                    SUM(CASE WHEN patron_type = 'Student' THEN 1 ELSE 0 END) AS student_loans,
                    SUM(CASE WHEN patron_type = 'Faculty' THEN 1 ELSE 0 END) AS faculty_loans,
                    SUM(CASE WHEN patron_type = 'Staff'   THEN 1 ELSE 0 END) AS staff_loans,
                    SUM(CASE WHEN patron_type = 'Alumni'  THEN 1 ELSE 0 END) AS alumni_loans,
                    SUM(CASE
                          WHEN patron_type NOT IN ('Student','Faculty','Staff','Alumni')
                          THEN 1 ELSE 0
                        END) AS other_loans,
                    COUNT(*) AS total_loans
                  FROM MonthlyTypeLoans
                  GROUP BY loan_month
                )
                SELECT
                  loan_month,
                  student_loans,
                  faculty_loans,
                  staff_loans,
                  alumni_loans,
                  other_loans,
                  total_loans,
                  SUM(total_loans) OVER (ORDER BY loan_month) AS running_total_loans
                FROM MonthlyAgg
                ORDER BY loan_month;
            """)
            monthly_data = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_monthly_loans.html", monthly_data=monthly_data)

# -----------------------------
# Q30: Co-author relationships
# -----------------------------
@app.route("/analytics/co-authors")
def co_authors():
    conn = get_connection()
    coauthors = []
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  a1.author_id AS author1_id,
                  CONCAT(a1.first_name, ' ', a1.last_name) AS author1_name,
                  a2.author_id AS author2_id,
                  CONCAT(a2.first_name, ' ', a2.last_name) AS author2_name,
                  COUNT(DISTINCT b.isbn) AS books_together
                FROM Author a1
                JOIN BookAuthor ba1 ON a1.author_id = ba1.author_id
                JOIN BookAuthor ba2 ON ba1.isbn = ba2.isbn
                JOIN Author a2 ON ba2.author_id = a2.author_id
                JOIN Book b ON ba1.isbn = b.isbn
                WHERE a1.author_id < a2.author_id
                GROUP BY a1.author_id, author1_name, a2.author_id, author2_name
                HAVING books_together >= 2
                ORDER BY books_together DESC, author1_name, author2_name
            """)
            coauthors = cur.fetchall()
    finally:
        conn.close()
    
    return render_template("analytics_co_authors.html", coauthors=coauthors)

if __name__ == "__main__":
    app.run(debug=True)
