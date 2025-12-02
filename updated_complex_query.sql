-- =========================================================
-- DASHBOARD MEGA SUMMARY
--   - total books, total copies, unique subjects
--   - total patrons + active patrons (have loan in last 90 days)
--   - total loans, current loans, overdue loans
--   - global avg loan duration
--   - loans created and returned in last 7 days
-- =========================================================

SET @today := CURDATE();
SET @last_7 := DATE_SUB(@today, INTERVAL 7 DAY);
SET @last_90 := DATE_SUB(@today, INTERVAL 90 DAY);

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

-- =========================================================
-- DASHBOARD OVERDUE ALERT WITH RISK SCORE
--   For each overdue loan:
--     - patron + book info
--     - days overdue
--     - total previous loans
--     - lifetime overdue count for patron
--     - unpaid fines
--     - computed "risk_score"
--     - rank of risk per overdue loan
-- =========================================================

SET @today := CURDATE();

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
  CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
  b.title AS book_title,
  DATE(o.due_ts) AS due_date,
  o.days_overdue,
  ph.all_loans,
  ph.overdue_or_late_loans,
  pfs.unpaid_fines,
  -- risk_score: weighted combination of days overdue, unpaid_fines, history
  (
    o.days_overdue * 1.0
    + ph.overdue_or_late_loans * 2.0
    + (pfs.unpaid_fines / 10.0)
  ) AS risk_score,
  RANK() OVER (
    ORDER BY
      (
        o.days_overdue * 1.0
        + ph.overdue_or_late_loans * 2.0
        + (pfs.unpaid_fines / 10.0)
      ) DESC
  ) AS risk_rank
FROM OverdueLoans o
JOIN Patron p ON o.patron_id = p.patron_id
JOIN Copy   c ON o.copy_id   = c.copy_id
JOIN Book   b ON c.isbn      = b.isbn
LEFT JOIN PatronHistory    ph  ON p.patron_id = ph.patron_id
LEFT JOIN PatronFineSummary pfs ON p.patron_id = pfs.patron_id
ORDER BY risk_score DESC, o.days_overdue DESC;

-- =========================================================
-- BOOKS CATALOG – ADVANCED SEARCH + SUBJECT RANK
--   - supports text search @search (title/ISBN)
--   - optional @subject_id filter
--   - returns popularity, availability
--   - ranks each book within its primary subject by loans
-- =========================================================

SET @search := NULL;      -- e.g. 'Data'
SET @subject_id := NULL;  -- e.g. 3

WITH BookCore AS (
  SELECT
    b.isbn,
    b.title,
    b.pub_year,
    b.publisher_id,
    MIN(bs.subject_id) AS primary_subject_id  -- pick smallest id as "primary"
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
  -- rank of this book within its primary subject by times_loaned
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
  (
    @search IS NULL OR @search = ''
    OR bd.title LIKE CONCAT('%', @search, '%')
    OR bd.isbn  LIKE CONCAT('%', @search, '%')
  )
  AND (
    @subject_id IS NULL
    OR bd.primary_subject_id = @subject_id
  )
ORDER BY
  COALESCE(bp.times_loaned, 0) DESC,
  bd.title;

-- =========================================================
-- PATRONS – ACTIVITY & RISK PROFILE
--   - total loans, active loans, overdue loans
--   - last loan date
--   - fines (total / unpaid)
--   - computed risk_level (LOW / MEDIUM / HIGH)
-- =========================================================

SET @recent_cutoff := DATE_SUB(CURDATE(), INTERVAL 30 DAY);

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
  CONCAT(p.first_name, ' ', p.last_name) AS patron_name,
  p.email,
  p.patron_type,
  p.balance,
  COALESCE(la.total_loans, 0) AS total_loans,
  COALESCE(la.active_loans, 0) AS active_loans,
  COALESCE(la.overdue_loans, 0) AS overdue_loans,
  la.last_loan_ts,
  COALESCE(fa.total_fines, 0)   AS total_fines,
  COALESCE(fa.unpaid_fines, 0)  AS unpaid_fines,
  -- classify risk level
  CASE
    WHEN COALESCE(fa.unpaid_fines, 0) >= 50
       OR COALESCE(la.overdue_loans, 0) >= 3
    THEN 'HIGH'
    WHEN COALESCE(fa.unpaid_fines, 0) BETWEEN 10 AND 49
       OR COALESCE(la.overdue_loans, 0) BETWEEN 1 AND 2
    THEN 'MEDIUM'
    ELSE 'LOW'
  END AS risk_level,
  -- flag whether they have borrowed in last 30 days
  CASE
    WHEN la.last_loan_ts IS NOT NULL
         AND DATE(la.last_loan_ts) >= @recent_cutoff THEN 1
    ELSE 0
  END AS is_recent_borrower
FROM Patron p
LEFT JOIN LoanAgg la ON p.patron_id = la.patron_id
LEFT JOIN FineAgg fa ON p.patron_id = fa.patron_id
ORDER BY
  risk_level DESC,  -- HIGH > MEDIUM > LOW
  unpaid_fines DESC,
  patron_name;

-- =========================================================
-- LOANS – SEGMENTED VIEW WITH BRANCH & TREND INFO
--   @status_filter: 'ALL' | 'CURRENT' | 'OVERDUE' | 'RETURNED'
-- =========================================================

SET @status_filter := 'ALL';  -- change for testing

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
  -- average loan duration per patron
  SELECT
    patron_id,
    AVG(duration_days) AS avg_duration_per_patron
  FROM LoanEnriched
  GROUP BY patron_id
)
SELECT
  le.loan_id,
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
  @status_filter = 'ALL'
  OR (@status_filter = 'CURRENT'  AND le.status = 'CURRENT')
  OR (@status_filter = 'OVERDUE'  AND le.status = 'OVERDUE')
  OR (@status_filter = 'RETURNED' AND le.status = 'RETURNED')
ORDER BY le.loan_ts DESC;

-- =========================================================
-- STATISTICS – MONTHLY LOAN TRENDS BY PATRON TYPE
--   Gives:
--     - loan_month (YYYY-MM)
--     - counts per patron_type (Student, Faculty, Staff, Alumni, Other)
--     - total loans
--     - running total over time
-- =========================================================

WITH MonthlyTypeLoans AS (
  SELECT
    DATE_FORMAT(l.loan_ts, '%Y-%m') AS loan_month,
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
