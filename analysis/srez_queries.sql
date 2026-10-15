-- Аналитический срез Tender Radar. Черновик запросов для разбора по токенам.
-- База: data/tender_radar.db (SQLite).

-- 1. Общий объем
SELECT COUNT(*) AS contracts,
       COUNT(DISTINCT published_at) AS days,
       MIN(published_at) AS first_day,
       MAX(published_at) AS last_day,
       ROUND(SUM(price) / 1e6) AS total_mln
FROM contracts
WHERE published_at IS NOT NULL;

-- 2. Доля контрактов без аванса
SELECT ROUND(100.0 * SUM(advance_pct = 0) / COUNT(*), 1) AS pct_no_advance
FROM contracts
WHERE advance_pct IS NOT NULL;

-- 3. Размер контрактов без аванса по диапазонам
SELECT CASE
         WHEN price < 100000    THEN '1. до 100 тыс'
         WHEN price < 500000    THEN '2. 100-500 тыс'
         WHEN price < 1000000   THEN '3. 500 тыс - 1 млн'
         WHEN price < 5000000   THEN '4. 1-5 млн'
         WHEN price < 20000000  THEN '5. 5-20 млн'
         WHEN price < 100000000 THEN '6. 20-100 млн'
         ELSE                        '7. свыше 100 млн'
       END AS bucket,
       COUNT(*) AS n,
       ROUND(SUM(price) / 1e6, 1) AS sum_mln
FROM contracts
WHERE advance_pct = 0 AND price > 0
GROUP BY bucket
ORDER BY bucket;

-- 4. Адресуемый сегмент: без аванса и от 1 млн
SELECT COUNT(*) AS n, ROUND(SUM(price) / 1e6) AS sum_mln
FROM contracts
WHERE advance_pct = 0 AND price >= 1000000;

-- 5. Доля без аванса по неделям (устойчивость показателя)
SELECT strftime('%Y-%W', published_at) AS week,
       COUNT(*) AS n,
       ROUND(100.0 * SUM(advance_pct = 0) / COUNT(*), 1) AS pct_no_advance
FROM contracts
WHERE published_at IS NOT NULL AND advance_pct IS NOT NULL
GROUP BY week
ORDER BY week;

-- 6. Срок исполнения в адресуемом сегменте
SELECT CASE
         WHEN d < 30  THEN '1. до 1 мес'
         WHEN d < 90  THEN '2. 1-3 мес'
         WHEN d < 180 THEN '3. 3-6 мес'
         WHEN d < 365 THEN '4. 6-12 мес'
         ELSE              '5. свыше года'
       END AS srok,
       COUNT(*) AS n
FROM (SELECT julianday(execution_until) - julianday(signed_at) AS d
      FROM contracts
      WHERE advance_pct = 0 AND price >= 1000000
        AND execution_until IS NOT NULL AND signed_at IS NOT NULL)
GROUP BY srok
ORDER BY srok;

-- 7. Способ определения поставщика в адресуемом сегменте
SELECT procurement_method, COUNT(*) AS n, ROUND(SUM(price) / 1e6) AS sum_mln
FROM contracts
WHERE advance_pct = 0 AND price >= 1000000
GROUP BY procurement_method
ORDER BY n DESC;

-- 8. Тип предмета контракта (товары / работы / услуги) по ключевым словам
SELECT CASE
         WHEN lower(subject) LIKE '%поставк%' THEN 'товары'
         WHEN lower(subject) LIKE '%выполнен%' OR lower(subject) LIKE '%ремонт%'
           OR lower(subject) LIKE '%строител%' OR lower(subject) LIKE '%работ%' THEN 'работы'
         WHEN lower(subject) LIKE '%оказан%' OR lower(subject) LIKE '%услуг%' THEN 'услуги'
         ELSE 'не определено'
       END AS tip,
       COUNT(*) AS n,
       ROUND(SUM(price) / 1e6) AS sum_mln,
       ROUND(AVG(julianday(execution_until) - julianday(signed_at))) AS avg_days
FROM contracts
WHERE advance_pct = 0 AND price >= 1000000
GROUP BY tip
ORDER BY n DESC;
