-- =====================================================================
-- Data quality degradation demo on OMOP CDM (Synthea, PostgreSQL)
--
--   Baseline : original clean schema  -> "cdm" (rename if different)
--   Degraded : schema "degraded" (a copy of the baseline), modified in place
--
-- Part 1 degrades the "degraded" schema directly; run your quality checks
-- against it afterwards. It is not reversible: re-copy the schema to reset.
-- Part 2 runs the same analysis on both schemas for comparison.
--
-- Degradation -> check it targets
--   D1  Unmapped T2DM diagnoses         -> C1.2  (and CC4.2)
--   D2  HbA1c stored in mmol/mol as %   -> P3.2  (and CC4.2)
--   D3  Sentinel birth date 1900-01-01  -> P3.1
--
-- Concept IDs:
--   201826   Type 2 diabetes mellitus
--   3004410  Hemoglobin A1c/Hemoglobin.total in Blood (LOINC 4548-4)
-- =====================================================================


-- =====================================================================
-- PART 1: Apply all degradations in one batch
-- =====================================================================
BEGIN;

-- D1  Completeness / mapping: half of T2DM patients have their diagnoses
--     unmapped (concept_id 0); source values are kept.
UPDATE degraded.condition_occurrence
SET    condition_concept_id = 0
WHERE  condition_concept_id IN (SELECT descendant_concept_id
                                FROM degraded.concept_ancestor
                                WHERE ancestor_concept_id = 201826)
  AND  abs(hashtext('unmapped' || person_id::text)::bigint) % 100 < 50;

-- D2  Conformance / units: HbA1c after 2015 converted to mmol/mol
--     but still labelled as percent.
UPDATE degraded.measurement
SET    value_as_number = ROUND(((value_as_number - 2.15) * 10.929)::numeric, 0)
WHERE  measurement_concept_id = 3004410
  AND  value_as_number IS NOT NULL
  AND  measurement_date >= DATE '2015-01-01';

-- D3  Plausibility / sentinel values: 25% of persons get a default
--     birth date of 1900-01-01.
UPDATE degraded.person
SET    year_of_birth  = 1900,
       month_of_birth = 1,
       day_of_birth   = 1,
       birth_datetime = TIMESTAMP '1900-01-01 00:00:00'
WHERE  abs(hashtext('dob' || person_id::text)::bigint) % 100 < 25;

COMMIT;


-- =====================================================================
-- PART 2: Analysis
-- Run the two queries below once per schema by changing search_path.
-- =====================================================================

--- SET search_path TO cdm;        -- baseline
SET search_path TO degraded;   -- degraded

-- 2a. Overall: T2DM prevalence, mean last HbA1c, % uncontrolled (>= 7)
WITH t2dm AS (
    SELECT DISTINCT co.person_id
    FROM condition_occurrence co
             JOIN concept_ancestor ca ON ca.descendant_concept_id = co.condition_concept_id
    WHERE ca.ancestor_concept_id = 201826
),
     last_hba1c AS (
         SELECT DISTINCT ON (person_id) person_id, value_as_number
         FROM measurement
         WHERE measurement_concept_id = 3004410 AND value_as_number IS NOT NULL
         ORDER BY person_id, measurement_date DESC
     )
SELECT
    (SELECT COUNT(*) FROM person)                                         AS n_persons,
    (SELECT COUNT(*) FROM t2dm)                                           AS n_t2dm,
    ROUND(100.0 * (SELECT COUNT(*) FROM t2dm) / (SELECT COUNT(*) FROM person), 2) AS t2dm_prev_pct,
    ROUND(AVG(h.value_as_number)::numeric, 2)                             AS mean_last_hba1c,
    ROUND(100.0 * AVG((h.value_as_number >= 7)::int), 1)                  AS pct_uncontrolled
FROM t2dm t
         JOIN last_hba1c h USING (person_id);

-- 2b. T2DM prevalence by age group (age at end of last observation period)
WITH t2dm AS (
    SELECT DISTINCT co.person_id
    FROM condition_occurrence co
             JOIN concept_ancestor ca ON ca.descendant_concept_id = co.condition_concept_id
    WHERE ca.ancestor_concept_id = 201826
),
     ages AS (
         SELECT p.person_id,
                EXTRACT(YEAR FROM MAX(op.observation_period_end_date))::int - p.year_of_birth AS age
         FROM person p
                  JOIN observation_period op USING (person_id)
         GROUP BY p.person_id, p.year_of_birth
     )
SELECT CASE WHEN age < 18 THEN '00-17'
            WHEN age < 45 THEN '18-44'
            WHEN age < 65 THEN '45-64'
            WHEN age < 85 THEN '65-84'
            ELSE '85+' END                                  AS age_group,
       COUNT(*)                                             AS n_persons,
       COUNT(t.person_id)                                   AS n_t2dm,
       ROUND(100.0 * COUNT(t.person_id) / COUNT(*), 2)      AS t2dm_prev_pct
FROM ages a
         LEFT JOIN t2dm t USING (person_id)
GROUP BY 1
ORDER BY 1;
