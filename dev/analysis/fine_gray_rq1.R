#!/usr/bin/env Rscript

# RQ1 competing-risks analysis with Fine-Gray models.
# This script consumes the compact weighted CSV exported by
# dev/analysis/fine_gray_rq1.py.

library(survival)
library(ggplot2)
library(systemfonts)


## Analysis constants setup

defaultInputDir <- "dev/analysis/output_rq1"
defaultOutputDir <- defaultInputDir
defaultMaxPlotDays <- 365
defaultTimePoints <- "30,90,180,365"
defaultEventTaxonomy <- "maintenance_class"
defaultMaintenanceClassEventTypes <- "adaptive,corrective,management,perfective,preventive"
defaultOperationalIntentEventTypes <- "security_fix,revert,bug_fix,dependency_update,merge_release_versioning,performance,feature,refactor_cleanup,test,documentation,resource,build_config_ci,style_formatting"
defaultFineGrayExpansion <- "by_repo"
defaultModelNames <- "file_role"
minimumSurvivalDays <- 1e-4

args <- commandArgs(trailingOnly = TRUE)

if ("--help" %in% args || "-h" %in% args) {
  cat("Usage:\n")
  cat("  Rscript dev/analysis/fine_gray_rq1.R [--input-dir DIR] [--input-file CSV] [--output-dir DIR] [--event-taxonomy maintenance_class|operational_intent] [--event-types CSV] [--time-points CSV] [--max-plot-days DAYS] [--finegray-expansion by_repo|global] [--model-names CSV] [--rds-dir DIR] [--skip-save-rds] [--cif-only]\n\n")
  cat("Defaults:\n")
  cat("  --input-dir ", defaultInputDir, "\n", sep = "")
  cat("  --output-dir ", defaultOutputDir, "\n", sep = "")
  cat("  --event-taxonomy ", defaultEventTaxonomy, "\n", sep = "")
  cat("  --event-types maintenance_class: ", defaultMaintenanceClassEventTypes, "\n", sep = "")
  cat("  --event-types operational_intent: ", defaultOperationalIntentEventTypes, "\n", sep = "")
  cat("  --time-points ", defaultTimePoints, "\n", sep = "")
  cat("  --max-plot-days ", defaultMaxPlotDays, "\n", sep = "")
  cat("  --finegray-expansion ", defaultFineGrayExpansion, "\n", sep = "")
  cat("  --model-names ", defaultModelNames, " (allowed: origin_only,file_role)\n", sep = "")
  cat("  --rds-dir DIR defaults to OUTPUT_DIR/rds_models\n")
  cat("  --skip-save-rds do not save fitted model objects\n")
  cat("  --cif-only skip Fine-Gray model fitting and only write CIF estimates/plots\n")
  quit(save = "no", status = 0)
}

getArgValue <- function(flag, defaultValue) {
  if (!(flag %in% args)) {
    return(defaultValue)
  }
  valueIndex <- match(flag, args) + 1
  if (valueIndex > length(args)) {
    stop(paste(flag, "requires a value"))
  }
  args[[valueIndex]]
}

splitCsvArg <- function(value) {
  parts <- trimws(unlist(strsplit(as.character(value), ",")))
  parts[parts != ""]
}

defaultInputFileForTaxonomy <- function(inputDir, eventTaxonomy) {
  file.path(inputDir, paste0("rq1_fine_gray_", eventTaxonomy, "_line_data.csv"))
}

defaultEventTypesForTaxonomy <- function(eventTaxonomy) {
  if (eventTaxonomy == "maintenance_class") {
    return(defaultMaintenanceClassEventTypes)
  }
  if (eventTaxonomy == "operational_intent") {
    return(defaultOperationalIntentEventTypes)
  }
  stop(paste("unsupported event taxonomy:", eventTaxonomy))
}

outputStemForTaxonomy <- function(eventTaxonomy) {
  if (eventTaxonomy == "maintenance_class") {
    return("rq1_fine_gray")
  }
  paste0("rq1_fine_gray_", eventTaxonomy)
}

inputDir <- getArgValue("--input-dir", defaultInputDir)
eventTaxonomy <- getArgValue("--event-taxonomy", defaultEventTaxonomy)
if (!(eventTaxonomy %in% c("maintenance_class", "operational_intent"))) {
  stop("--event-taxonomy must be one of: maintenance_class, operational_intent")
}
inputFile <- getArgValue(
  "--input-file",
  defaultInputFileForTaxonomy(inputDir, eventTaxonomy)
)
outputDir <- getArgValue("--output-dir", defaultOutputDir)
eventTypes <- splitCsvArg(getArgValue("--event-types", defaultEventTypesForTaxonomy(eventTaxonomy)))
timePoints <- as.numeric(splitCsvArg(getArgValue("--time-points", defaultTimePoints)))
maxPlotDays <- as.numeric(getArgValue("--max-plot-days", defaultMaxPlotDays))
fineGrayExpansion <- getArgValue("--finegray-expansion", defaultFineGrayExpansion)
modelNames <- splitCsvArg(getArgValue("--model-names", defaultModelNames))
rdsDir <- getArgValue("--rds-dir", file.path(outputDir, "rds_models"))
saveRdsModels <- !("--skip-save-rds" %in% args)
skipFineGrayModels <- "--cif-only" %in% args
outputStem <- outputStemForTaxonomy(eventTaxonomy)

if (!file.exists(inputFile)) {
  stop(paste("input file does not exist:", inputFile))
}
if (length(eventTypes) == 0) {
  stop("--event-types must include at least one event type")
}
if (any(is.na(timePoints)) || any(timePoints < 0)) {
  stop("--time-points must be comma-separated non-negative numbers")
}
if (is.na(maxPlotDays) || maxPlotDays <= 0) {
  stop("--max-plot-days must be a positive number")
}
if (!(fineGrayExpansion %in% c("by_repo", "global"))) {
  stop("--finegray-expansion must be one of: by_repo, global")
}
if (length(modelNames) == 0) {
  stop("--model-names must include at least one model")
}
invalidModelNames <- setdiff(modelNames, c("origin_only", "file_role"))
if (length(invalidModelNames) > 0) {
  stop(paste("--model-names has unsupported model(s):", paste(invalidModelNames, collapse = ", ")))
}

dir.create(outputDir, recursive = TRUE, showWarnings = FALSE)
if (saveRdsModels && !skipFineGrayModels) {
  dir.create(rdsDir, recursive = TRUE, showWarnings = FALSE)
}

formatElapsed <- function(startTime) {
  paste0(round(as.numeric(difftime(Sys.time(), startTime, units = "secs")), 2), "s")
}

logMessage <- function(...) {
  cat("[", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), "] ", paste0(...), "\n", sep = "")
  flush.console()
}

logStageDone <- function(label, startTime) {
  logMessage(label, " finished elapsed=", formatElapsed(startTime))
}

safeFileStem <- function(value) {
  stem <- gsub("[^A-Za-z0-9_.-]+", "_", as.character(value))
  stem <- gsub("^_+|_+$", "", stem)
  if (!nzchar(stem)) {
    return("model")
  }
  stem
}

saveFittedFineGrayModel <- function(fitResult, eventType, modelName) {
  if (!saveRdsModels || fitResult$status != "fit") {
    fitResult$rds_path <- NA_character_
    return(fitResult)
  }

  rdsPath <- file.path(
    rdsDir,
    paste0(
      outputStem,
      "_",
      safeFileStem(eventType),
      "_",
      safeFileStem(modelName),
      ".rds"
    )
  )
  saved <- tryCatch(
    {
      saveRDS(fitResult$fit, rdsPath)
      TRUE
    },
    error = function(err) err
  )
  if (inherits(saved, "error")) {
    logMessage(
      "Failed to save model RDS: event_type=", eventType,
      " model=", modelName,
      " path=", rdsPath,
      " reason=", conditionMessage(saved)
    )
    fitResult$rds_path <- NA_character_
    fitResult$rds_error <- conditionMessage(saved)
    return(fitResult)
  }

  logMessage(
    "Saved model RDS: event_type=", eventType,
    " model=", modelName,
    " path=", rdsPath
  )
  fitResult$rds_path <- rdsPath
  fitResult$rds_error <- ""
  fitResult
}

scriptStartTime <- Sys.time()
logMessage("fine_gray_rq1.R starting")
logMessage("  input_file: ", inputFile)
logMessage("  output_dir: ", outputDir)
logMessage("  event_taxonomy: ", eventTaxonomy)
logMessage("  event_types_requested: ", paste(eventTypes, collapse = ", "))
logMessage("  time_points: ", paste(timePoints, collapse = ", "))
logMessage("  max_plot_days: ", maxPlotDays)
logMessage("  finegray_expansion: ", fineGrayExpansion)
logMessage("  model_names: ", paste(modelNames, collapse = ", "))
logMessage("  save_rds_models: ", saveRdsModels)
if (saveRdsModels) {
  logMessage("  rds_dir: ", rdsDir)
}
logMessage("  cif_only: ", skipFineGrayModels)

roleColors <- c(
  not_ai_coauthored = "#8789C0",
  ai_coauthored = "#F58F29"
)
roleLabels <- c(
  not_ai_coauthored = "Human",
  ai_coauthored = "Agentic"
)

eventTypeLabel <- function(value) {
  label <- gsub("_", " ", as.character(value), fixed = TRUE)
  label <- tools::toTitleCase(label)
  label
}

eventTypePalette <- function(eventTypes) {
  baseColors <- c(
    "#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00",
    "#56B4E9", "#F0E442", "#000000", "#8C564B", "#9467BD",
    "#17BECF", "#BCBD22", "#7F7F7F", "#1F77B4", "#FF7F0E"
  )
  if (length(eventTypes) <= length(baseColors)) {
    colors <- baseColors[seq_along(eventTypes)]
  } else {
    colors <- grDevices::hcl.colors(length(eventTypes), palette = "Dark 3")
  }
  stats::setNames(colors, eventTypes)
}

plotFontFamily <- "Nimbus Roman"
fontDir <- file.path(getwd(), "font")
regularFont <- file.path(fontDir, "NimbusRomNo9L-Reg.otf")
boldFont <- file.path(fontDir, "NimbusRomNo9L-Med.otf")
italicFont <- file.path(fontDir, "NimbusRomNo9L-RegIta.otf")
boldItalicFont <- file.path(fontDir, "NimbusRomNo9L-MedIta.otf")
missingFontFiles <- c(regularFont, boldFont, italicFont, boldItalicFont)[
  !file.exists(c(regularFont, boldFont, italicFont, boldItalicFont))
]
if (length(missingFontFiles) > 0) {
  stop(paste("missing Nimbus Roman font file(s):", paste(missingFontFiles, collapse = ", ")))
}
if (!(plotFontFamily %in% systemfonts::system_fonts()$family)) {
  systemfonts::register_font(
    name = plotFontFamily,
    plain = regularFont,
    bold = boldFont,
    italic = italicFont,
    bolditalic = boldItalicFont
  )
}
cairoPdfDevice <- function(filename, width, height, ...) {
  grDevices::cairo_pdf(
    filename = filename,
    width = width,
    height = height,
    family = plotFontFamily,
    ...
  )
}


## Input loading

loadFineGrayInput <- function(csvPath) {
  loadStartTime <- Sys.time()
  logMessage("Loading Fine-Gray input CSV: ", csvPath)
  line_lifecycle <- read.csv(csvPath, stringsAsFactors = FALSE)
  logMessage(
    "Loaded raw rows: ",
    format(nrow(line_lifecycle), big.mark = ","),
    " elapsed=",
    formatElapsed(loadStartTime)
  )
  requiredColumns <- c(
    "repo",
    "origin_commit_category",
    "file_primary_role",
    "survival_days_positive",
    "event_type_factor",
    "event_observed",
    "weight"
  )
  missingColumns <- setdiff(requiredColumns, colnames(line_lifecycle))
  if (length(missingColumns) > 0) {
    stop(paste("missing required columns:", paste(missingColumns, collapse = ", ")))
  }

  line_lifecycle$repo <- as.factor(line_lifecycle$repo)
  line_lifecycle$origin_commit_category <- factor(
    line_lifecycle$origin_commit_category,
    levels = c("not_ai_coauthored", "ai_coauthored")
  )
  line_lifecycle$file_primary_role <- as.factor(line_lifecycle$file_primary_role)
  line_lifecycle$survival_days_positive <- as.numeric(line_lifecycle$survival_days_positive)
  line_lifecycle$event_observed <- as.integer(line_lifecycle$event_observed)
  line_lifecycle$weight <- as.numeric(line_lifecycle$weight)
  line_lifecycle$event_type_factor <- as.character(line_lifecycle$event_type_factor)
  line_lifecycle$event_type_factor[line_lifecycle$event_observed == 0] <- "censored"

  eventLevels <- c(
    "censored",
    sort(setdiff(unique(line_lifecycle$event_type_factor), "censored"))
  )
  line_lifecycle$event_type_factor <- factor(line_lifecycle$event_type_factor, levels = eventLevels)

  beforeFilterRows <- nrow(line_lifecycle)
  line_lifecycle <- line_lifecycle[
    !is.na(line_lifecycle$repo) &
      !is.na(line_lifecycle$origin_commit_category) &
      !is.na(line_lifecycle$file_primary_role) &
      !is.na(line_lifecycle$survival_days_positive) &
      !is.na(line_lifecycle$event_type_factor) &
      !is.na(line_lifecycle$event_observed) &
      !is.na(line_lifecycle$weight) &
      line_lifecycle$survival_days_positive > 0 &
      line_lifecycle$event_observed %in% c(0L, 1L) &
      line_lifecycle$weight > 0,
  ]
  droppedRows <- beforeFilterRows - nrow(line_lifecycle)
  if (droppedRows > 0) {
    logMessage(
      "Dropped invalid input rows: ",
      format(droppedRows, big.mark = ",")
    )
  }

  line_lifecycle$survival_days_positive <- pmax(
    line_lifecycle$survival_days_positive,
    minimumSurvivalDays
    # here is where we floor the survival days, 
  )

  line_lifecycle$repo <- droplevels(line_lifecycle$repo)
  line_lifecycle$origin_commit_category <- droplevels(line_lifecycle$origin_commit_category)
  line_lifecycle$file_primary_role <- droplevels(line_lifecycle$file_primary_role)
  line_lifecycle$event_type_factor <- droplevels(line_lifecycle$event_type_factor)
  logMessage(
    "Finished loading/filtering input: rows=",
    format(nrow(line_lifecycle), big.mark = ","),
    " weighted_lines=",
    format(round(sum(line_lifecycle$weight)), big.mark = ","),
    " repos=",
    length(unique(line_lifecycle$repo)),
    " event_levels=",
    paste(levels(line_lifecycle$event_type_factor), collapse = ", "),
    " elapsed=",
    formatElapsed(loadStartTime)
  )
  line_lifecycle
}

line_lifecycle <- loadFineGrayInput(inputFile)


## CIF estimation and plotting

buildCifAggregate <- function(data) {
  if (requireNamespace("data.table", quietly = TRUE)) {
    dt <- data.table::as.data.table(data)
    aggregateData <- dt[
      ,
      list(weight = sum(weight)),
      by = c(
        "origin_commit_category",
        "survival_days_positive",
        "event_observed",
        "event_type_factor"
      )
    ]
    return(as.data.frame(aggregateData))
  }

  aggregate(
    weight ~ origin_commit_category + survival_days_positive + event_observed + event_type_factor,
    data = data,
    FUN = sum
  )
}

weightedCifCurveFromAggregate <- function(aggregateData, totalWeight, eventType) {
  if (nrow(aggregateData) == 0 || totalWeight <= 0) {
    return(data.frame(time = 0, cif = 0))
  }

  if (requireNamespace("data.table", quietly = TRUE)) {
    dt <- data.table::as.data.table(aggregateData)
    dt[, event_type_char := as.character(event_type_factor)]
    byTime <- dt[
      ,
      list(
        event_weight = sum(weight[event_observed == 1]),
        event_type_weight = sum(weight[event_observed == 1 & event_type_char == eventType]),
        censored_weight = sum(weight[event_observed == 0])
      ),
      by = survival_days_positive
    ]
    data.table::setorder(byTime, survival_days_positive)
    byTime <- as.data.frame(byTime)
  } else {
    timeData <- data.frame(
      survival_days_positive = aggregateData$survival_days_positive,
      event_weight = ifelse(aggregateData$event_observed == 1, aggregateData$weight, 0),
      event_type_weight = ifelse(
        aggregateData$event_observed == 1 &
          as.character(aggregateData$event_type_factor) == eventType,
        aggregateData$weight,
        0
      ),
      censored_weight = ifelse(aggregateData$event_observed == 0, aggregateData$weight, 0)
    )
    byTime <- aggregate(
      cbind(event_weight, event_type_weight, censored_weight) ~ survival_days_positive,
      data = timeData,
      FUN = sum
    )
    byTime <- byTime[order(byTime$survival_days_positive), , drop = FALSE]
  }

  removedWeight <- byTime$event_weight + byTime$censored_weight
  priorRemovedWeight <- c(0, head(cumsum(removedWeight), -1))
  risk <- totalWeight - priorRemovedWeight
  validRows <- is.finite(risk) & risk > 0
  if (!any(validRows)) {
    return(data.frame(time = 0, cif = 0))
  }

  byTime <- byTime[validRows, , drop = FALSE]
  risk <- risk[validRows]
  eventHazard <- byTime$event_weight / risk
  targetHazard <- byTime$event_type_weight / risk
  survivalBefore <- c(1, head(cumprod(1 - eventHazard), -1))
  cif <- cumsum(survivalBefore * targetHazard)

  data.frame(
    time = c(0, byTime$survival_days_positive),
    cif = c(0, cif)
  )
}

buildCifCurveCache <- function(data, eventTypes) {
  cifStartTime <- Sys.time()
  logMessage("Building CIF aggregate table...")
  aggregateData <- buildCifAggregate(data)
  logMessage(
    "CIF aggregate rows: ",
    format(nrow(aggregateData), big.mark = ","),
    " elapsed=",
    formatElapsed(cifStartTime)
  )

  curves <- list()
  for (eventType in eventTypes) {
    curves[[eventType]] <- list()
    for (category in levels(data$origin_commit_category)) {
      curveStartTime <- Sys.time()
      categoryData <- aggregateData[
        aggregateData$origin_commit_category == category,
        ,
        drop = FALSE
      ]
      totalWeight <- sum(data$weight[data$origin_commit_category == category])
      if (nrow(categoryData) == 0 || totalWeight <= 0) {
        next
      }
      logMessage(
        "Computing CIF curve: event_type=", eventType,
        " origin=", category,
        " aggregate_rows=", format(nrow(categoryData), big.mark = ",")
      )
      curves[[eventType]][[category]] <- weightedCifCurveFromAggregate(
        categoryData,
        totalWeight,
        eventType
      )
      logStageDone(
        paste0(
          "CIF curve event_type=", eventType,
          " origin=", category,
          " time_points=", format(nrow(curves[[eventType]][[category]]), big.mark = ",")
        ),
        curveStartTime
      )
    }
  }
  logStageDone("CIF curve cache", cifStartTime)
  curves
}

estimateCifAtTime <- function(curve, timePoint) {
  eligible <- curve[curve$time <= timePoint, , drop = FALSE]
  if (nrow(eligible) == 0) {
    return(0)
  }
  tail(eligible$cif, 1)
}

cifPlotIntervalDays <- 7
cifTextSize <- 18
cifGeomTextSize <- cifTextSize / ggplot2::.pt

resampledCifCurve <- function(curve, maxPlotDays, intervalDays = cifPlotIntervalDays) {
  if (is.null(curve) || nrow(curve) == 0 || !is.finite(maxPlotDays) || maxPlotDays <= 0) {
    return(data.frame(time = numeric(), cif = numeric()))
  }

  sourceCurve <- curve[is.finite(curve$time) & is.finite(curve$cif), c("time", "cif"), drop = FALSE]
  if (nrow(sourceCurve) == 0) {
    return(data.frame(time = numeric(), cif = numeric()))
  }

  sourceCurve <- sourceCurve[order(sourceCurve$time), , drop = FALSE]
  sourceCurve <- aggregate(cif ~ time, data = sourceCurve, FUN = max)
  if (!any(sourceCurve$time <= 0)) {
    sourceCurve <- rbind(data.frame(time = 0, cif = 0), sourceCurve)
  }
  sourceCurve <- sourceCurve[order(sourceCurve$time), , drop = FALSE]

  plotTimes <- seq(0, maxPlotDays, by = intervalDays)
  if (tail(plotTimes, 1) < maxPlotDays) {
    plotTimes <- c(plotTimes, maxPlotDays)
  }
  plotValues <- stats::approx(
    x = sourceCurve$time,
    y = sourceCurve$cif,
    xout = plotTimes,
    method = "constant",
    f = 0,
    rule = 2,
    ties = "ordered"
  )$y

  data.frame(time = plotTimes, cif = plotValues)
}

buildCifEstimates <- function(cifCurves, eventTypes, timePoints) {
  rows <- list()
  rowIndex <- 1
  for (eventType in eventTypes) {
    for (category in names(cifCurves[[eventType]])) {
      curve <- cifCurves[[eventType]][[category]]
      if (is.null(curve) || nrow(curve) == 0) {
        next
      }
      for (timePoint in timePoints) {
        rows[[rowIndex]] <- data.frame(
          event_type = eventType,
          event_taxonomy = eventTaxonomy,
          origin_commit_category = category,
          origin_group = unname(roleLabels[[category]]),
          days = timePoint,
          cif = estimateCifAtTime(curve, timePoint),
          stringsAsFactors = FALSE
        )
        rowIndex <- rowIndex + 1
      }
    }
  }
  if (length(rows) == 0) {
    return(data.frame())
  }
  do.call(rbind, rows)
}

writeCifPlot <- function(cifCurves, eventType, outputPath, maxPlotDays, timePoints) {
  plotRows <- list()
  annotationRows <- list()
  rowIndex <- 1
  annotationIndex <- 1
  referenceTimes <- sort(unique(timePoints[is.finite(timePoints) & timePoints >= 0 & timePoints <= maxPlotDays]))
  for (category in names(cifCurves[[eventType]])) {
    curve <- cifCurves[[eventType]][[category]]
    if (is.null(curve) || nrow(curve) == 0) {
      next
    }
    plotCurve <- resampledCifCurve(curve, maxPlotDays)
    if (nrow(plotCurve) == 0) {
      next
    }
    plotCurve$origin_commit_category <- category
    plotCurve$origin_group <- unname(roleLabels[[category]])
    plotRows[[rowIndex]] <- plotCurve
    rowIndex <- rowIndex + 1
    for (referenceTime in referenceTimes) {
      annotationRows[[annotationIndex]] <- data.frame(
        days = referenceTime,
        cif = estimateCifAtTime(curve, referenceTime),
        origin_commit_category = category,
        origin_group = unname(roleLabels[[category]]),
        stringsAsFactors = FALSE
      )
      annotationIndex <- annotationIndex + 1
    }
  }
  if (length(plotRows) == 0) {
    return(FALSE)
  }
  plotData <- do.call(rbind, plotRows)
  plotData <- plotData[plotData$time <= maxPlotDays, , drop = FALSE]
  annotationData <- if (length(annotationRows) > 0) {
    do.call(rbind, annotationRows)
  } else {
    data.frame(
      days = numeric(),
      cif = numeric(),
      origin_commit_category = character(),
      origin_group = character(),
      label_y = numeric(),
      cif_label = character(),
      label_hjust = numeric(),
      stringsAsFactors = FALSE
    )
  }
  if (nrow(annotationData) > 0) {
    annotationData$cif_label <- scales::percent(annotationData$cif, accuracy = 0.1)
  }

  p <- ggplot(
    plotData,
    aes(
      x = time,
      y = cif,
      color = origin_commit_category,
      group = origin_commit_category
    )
    ) +
    geom_line(linewidth = 1.6, lineend = "round") +
    geom_point(
      data = annotationData,
      aes(
        x = days,
        y = cif,
        color = origin_commit_category
      ),
      inherit.aes = FALSE,
      size = 3.2,
      show.legend = FALSE
    ) +
    geom_text(
      data = annotationData,
      aes(
        x = days,
        y = cif,
        label = cif_label,
        color = origin_commit_category
      ),
      inherit.aes = FALSE,
      size = cifGeomTextSize,
      family = plotFontFamily,
      hjust = 0.5,
      vjust = 0.5,
      show.legend = FALSE
    ) +
    scale_color_manual(values = roleColors, labels = roleLabels, name = "") +
    scale_y_continuous(labels = scales::percent_format(accuracy = 1)) +
    coord_cartesian(xlim = c(0, maxPlotDays), ylim = c(0, NA)) +
    labs(
      x = "Days Since Merge",
      y = "Cumulative Incidence"
    ) +
    theme_classic(base_size = cifTextSize, base_family = plotFontFamily) +
    theme(
      text = element_text(family = plotFontFamily),
      legend.position = c(0.98, 0.08),
      legend.justification = c(1, 0),
      legend.background = element_blank(),
      legend.text = element_text(size = cifTextSize),
      legend.key.size = grid::unit(0.5, "cm"),
      axis.title = element_text(size = cifTextSize),
      axis.text = element_text(size = cifTextSize),
      plot.title = element_blank()
    )

  ggsave(
    outputPath,
    plot = p,
    width = 7.2,
    height = 3.2,
    units = "in",
    device = cairoPdfDevice
  )
  logMessage("Wrote CIF plot: ", outputPath)
  TRUE
}

writeCombinedCifPlot <- function(cifCurves, eventTypes, outputPath, maxPlotDays, timePoints) {
  plotRows <- list()
  rowIndex <- 1
  referenceTimes <- sort(unique(timePoints[is.finite(timePoints) & timePoints >= 0 & timePoints <= maxPlotDays]))
  for (eventType in eventTypes) {
    if (is.null(cifCurves[[eventType]])) {
      next
    }
    for (category in names(cifCurves[[eventType]])) {
      curve <- cifCurves[[eventType]][[category]]
      if (is.null(curve) || nrow(curve) == 0) {
        next
      }
      plotCurve <- resampledCifCurve(curve, maxPlotDays)
      if (nrow(plotCurve) == 0) {
        next
      }
      plotCurve$event_type <- eventType
      plotCurve$event_type_label <- eventTypeLabel(eventType)
      plotCurve$origin_commit_category <- category
      plotCurve$origin_group <- unname(roleLabels[[category]])
      plotRows[[rowIndex]] <- plotCurve
      rowIndex <- rowIndex + 1
    }
  }
  if (length(plotRows) == 0) {
    return(FALSE)
  }

  plotData <- do.call(rbind, plotRows)
  plotData$event_type <- factor(plotData$event_type, levels = eventTypes)
  plotData$event_type_label <- factor(
    plotData$event_type_label,
    levels = eventTypeLabel(eventTypes)
  )
  plotData$origin_commit_category <- factor(
    plotData$origin_commit_category,
    levels = names(roleLabels)
  )
  colorValues <- eventTypePalette(eventTypes)
  names(colorValues) <- eventTypeLabel(names(colorValues))
  linetypeValues <- c(
    not_ai_coauthored = "solid",
    ai_coauthored = "22"
  )
  colorLegendCols <- if (length(eventTypes) <= 6) 2 else 3

  p <- ggplot(
    plotData,
    aes(
      x = time,
      y = cif,
      color = event_type_label,
      linetype = origin_commit_category,
      group = interaction(event_type_label, origin_commit_category)
    )
  ) +
    geom_vline(
      xintercept = referenceTimes,
      color = "grey78",
      linewidth = 0.3,
      linetype = "dashed"
    ) +
    geom_line(linewidth = 1.15, alpha = 0.92, lineend = "round") +
    scale_color_manual(values = colorValues, name = "Terminal subtype") +
    scale_linetype_manual(
      values = linetypeValues,
      labels = roleLabels,
      name = "Origin"
    ) +
    scale_y_continuous(labels = scales::percent_format(accuracy = 1)) +
    coord_cartesian(xlim = c(0, maxPlotDays), ylim = c(0, NA)) +
    labs(
      x = "Days Since Merge",
      y = "Cumulative Incidence"
    ) +
    guides(
      color = guide_legend(
        ncol = colorLegendCols,
        keywidth = grid::unit(1.1, "cm"),
        override.aes = list(linetype = "solid", linewidth = 1.6)
      ),
      linetype = guide_legend(
        keywidth = grid::unit(2.4, "cm"),
        override.aes = list(color = "grey20", linewidth = 1.8)
      )
    ) +
    theme_classic(base_size = cifTextSize, base_family = plotFontFamily) +
    theme(
      text = element_text(family = plotFontFamily),
      legend.position = c(0.02, 0.98),
      legend.justification = c(0, 1),
      legend.box = "vertical",
      legend.background = element_rect(
        fill = grDevices::adjustcolor("white", alpha.f = 0.85),
        color = NA
      ),
      legend.margin = margin(4, 6, 4, 6),
      legend.title = element_text(size = cifTextSize),
      legend.text = element_text(size = cifTextSize),
      legend.key.height = grid::unit(0.35, "cm"),
      axis.title = element_text(size = cifTextSize),
      axis.text = element_text(size = cifTextSize),
      plot.title = element_blank()
    )

  plotHeight <- if (length(eventTypes) <= 6) 4.6 else 5.5
  plotWidth <- if (length(eventTypes) <= 6) 7.8 else 9.4
  ggsave(
    outputPath,
    plot = p,
    width = plotWidth,
    height = plotHeight,
    units = "in",
    device = cairoPdfDevice
  )
  logMessage("Wrote combined CIF plot: ", outputPath)
  TRUE
}


## Fine-Gray modeling

eventCount <- function(data, eventType) {
  sum(data$weight[data$event_observed == 1 & as.character(data$event_type_factor) == eventType])
}

sanitizeFineGrayData <- function(fgData) {
  validRows <- is.finite(fgData$fgstart) &
    is.finite(fgData$fgstop) &
    is.finite(fgData$fgwt) &
    fgData$fgwt > 0 &
    fgData$fgstop > fgData$fgstart
  list(
    data = fgData[validRows, , drop = FALSE],
    dropped_rows = sum(!validRows)
  )
}

compressFineGrayData <- function(fgData, modelName) {
  groupColumns <- c(
    "fgstart",
    "fgstop",
    "fgstatus",
    "origin_commit_category",
    "repo"
  )
  if (modelName == "file_role") {
    groupColumns <- c(groupColumns, "file_primary_role")
  }

  if (requireNamespace("data.table", quietly = TRUE)) {
    fgDt <- data.table::as.data.table(fgData)
    compressed <- fgDt[
      ,
      list(fgwt = sum(fgwt)),
      by = groupColumns
    ]
    return(as.data.frame(compressed))
  }

  aggregate(
    fgwt ~ .,
    data = fgData[, c(groupColumns, "fgwt"), drop = FALSE],
    FUN = sum
  )
}

bindFineGrayChunks <- function(chunks) {
  chunks <- chunks[!vapply(chunks, is.null, logical(1))]
  if (length(chunks) == 0) {
    return(data.frame())
  }
  if (requireNamespace("data.table", quietly = TRUE)) {
    return(as.data.frame(data.table::rbindlist(chunks, use.names = TRUE, fill = TRUE)))
  }
  do.call(rbind, chunks)
}

expandAndCompressFineGrayGlobal <- function(data, eventType, modelName, fgFormula) {
  expansionStartTime <- Sys.time()
  logMessage(
    "Fine-Gray global expansion start: event_type=", eventType,
    " model=", modelName,
    " input_rows=", format(nrow(data), big.mark = ",")
  )
  fgData <- tryCatch(
    finegray(
      fgFormula,
      data = data,
      etype = eventType,
      weights = weight
    ),
    error = function(err) err
  )
  if (inherits(fgData, "error")) {
    return(list(
      data = data.frame(),
      raw_fg_rows = 0,
      sanitized_fg_rows = 0,
      compressed_fg_rows = 0,
      dropped_fg_rows = 0,
      skipped_no_target_repos = 0,
      skipped_no_target_rows = 0,
      repo_errors = character(),
      global_error = fgData$message
    ))
  }

  rawFgRows <- nrow(fgData)
  sanitizedFgData <- sanitizeFineGrayData(fgData)
  fgData <- sanitizedFgData$data
  droppedFgRows <- sanitizedFgData$dropped_rows
  sanitizedFgRows <- nrow(fgData)

  compressedStartTime <- Sys.time()
  logMessage(
    "Compressing global Fine-Gray data: event_type=", eventType,
    " model=", modelName,
    " rows_before=", format(sanitizedFgRows, big.mark = ",")
  )
  if (nrow(fgData) > 0) {
    fgData <- compressFineGrayData(fgData, modelName)
  }
  compressedFgRows <- nrow(fgData)
  compressionRatio <- if (sanitizedFgRows > 0) compressedFgRows / sanitizedFgRows else NA_real_
  logMessage(
    "Finished global Fine-Gray compression: event_type=", eventType,
    " model=", modelName,
    " rows_after=", format(compressedFgRows, big.mark = ","),
    " retained=",
    ifelse(is.na(compressionRatio), "NA", paste0(round(100 * compressionRatio, 2), "%")),
    " elapsed=", formatElapsed(compressedStartTime)
  )
  logMessage(
    "Fine-Gray global expansion done: event_type=", eventType,
    " model=", modelName,
    " raw_fg_rows=", format(rawFgRows, big.mark = ","),
    " dropped=", format(droppedFgRows, big.mark = ","),
    " compressed_rows=", format(compressedFgRows, big.mark = ","),
    " elapsed=", formatElapsed(expansionStartTime)
  )

  list(
    data = fgData,
    raw_fg_rows = rawFgRows,
    sanitized_fg_rows = sanitizedFgRows,
    compressed_fg_rows = compressedFgRows,
    dropped_fg_rows = droppedFgRows,
    skipped_no_target_repos = 0,
    skipped_no_target_rows = 0,
    repo_errors = character(),
    global_error = ""
  )
}

expandAndCompressFineGrayByRepo <- function(data, eventType, modelName, fgFormula) {
  expansionStartTime <- Sys.time()
  repoLevels <- levels(data$repo)
  compressedChunks <- list()
  repoErrors <- character()
  rawFgRows <- 0
  sanitizedFgRows <- 0
  compressedFgRows <- 0
  droppedFgRows <- 0
  skippedNoTargetRepos <- 0
  skippedNoTargetRows <- 0
  chunkIndex <- 1
  repoCount <- length(repoLevels)
  logMessage(
    "Fine-Gray by-repo expansion start: event_type=", eventType,
    " model=", modelName,
    " repos=", repoCount,
    " input_rows=", format(nrow(data), big.mark = ",")
  )

  for (repoIndex in seq_along(repoLevels)) {
    repoLevel <- repoLevels[[repoIndex]]
    repoStartTime <- Sys.time()
    repoData <- data[data$repo == repoLevel, , drop = FALSE]
    if (nrow(repoData) == 0) {
      next
    }
    if (eventCount(repoData, eventType) <= 0) {
      skippedNoTargetRepos <- skippedNoTargetRepos + 1
      skippedNoTargetRows <- skippedNoTargetRows + nrow(repoData)
      rm(repoData)
      next
    }

    fgData <- tryCatch(
      finegray(
        fgFormula,
        data = repoData,
        etype = eventType,
        weights = weight
      ),
      error = function(err) err
    )
    if (inherits(fgData, "error")) {
      repoErrors <- c(repoErrors, paste0(repoLevel, ": ", fgData$message))
      logMessage(
        "Fine-Gray repo expansion skipped: event_type=", eventType,
        " model=", modelName,
        " repo=", repoLevel,
        " reason=", fgData$message,
        " elapsed=", formatElapsed(repoStartTime)
      )
      next
    }

    repoRawRows <- nrow(fgData)
    rawFgRows <- rawFgRows + repoRawRows
    fgData$repo <- factor(repoLevel, levels = repoLevels)

    sanitizedFgData <- sanitizeFineGrayData(fgData)
    fgData <- sanitizedFgData$data
    repoDroppedRows <- sanitizedFgData$dropped_rows
    droppedFgRows <- droppedFgRows + repoDroppedRows
    sanitizedFgRows <- sanitizedFgRows + nrow(fgData)

    if (nrow(fgData) > 0) {
      repoCompressed <- compressFineGrayData(fgData, modelName)
      compressedFgRows <- compressedFgRows + nrow(repoCompressed)
      compressedChunks[[chunkIndex]] <- repoCompressed
      chunkIndex <- chunkIndex + 1
    } else {
      repoCompressed <- data.frame()
    }

    rm(fgData, repoCompressed, repoData)
    if (repoIndex %% 10 == 0) {
      gc(verbose = FALSE)
    }
  }

  logMessage(
    "Fine-Gray by-repo expansion done: event_type=", eventType,
    " model=", modelName,
    " repos=", repoCount,
    " raw_fg_rows=", format(rawFgRows, big.mark = ","),
    " sanitized_fg_rows=", format(sanitizedFgRows, big.mark = ","),
    " compressed_fg_rows=", format(compressedFgRows, big.mark = ","),
    " dropped_fg_rows=", format(droppedFgRows, big.mark = ","),
    " skipped_no_target_repos=", skippedNoTargetRepos,
    " skipped_no_target_rows=", format(skippedNoTargetRows, big.mark = ","),
    " repo_errors=", length(repoErrors),
    " elapsed=", formatElapsed(expansionStartTime)
  )

  list(
    data = bindFineGrayChunks(compressedChunks),
    raw_fg_rows = rawFgRows,
    sanitized_fg_rows = sanitizedFgRows,
    compressed_fg_rows = compressedFgRows,
    dropped_fg_rows = droppedFgRows,
    skipped_no_target_repos = skippedNoTargetRepos,
    skipped_no_target_rows = skippedNoTargetRows,
    repo_errors = repoErrors
  )
}

fitFineGrayModel <- function(data, eventType, modelName) {
  if (!(eventType %in% levels(data$event_type_factor))) {
    return(list(status = "skipped", reason = "event_type_absent"))
  }
  if (eventCount(data, eventType) <= 0) {
    return(list(status = "skipped", reason = "no_events"))
  }
  if (length(unique(data$repo)) < 2) {
    return(list(status = "skipped", reason = "fewer_than_two_repo_clusters"))
  }
  if (nlevels(data$origin_commit_category) < 2) {
    return(list(status = "skipped", reason = "missing_origin_group_variation"))
  }

  if (modelName == "origin_only") {
    fgFormula <- if (fineGrayExpansion == "global") {
      Surv(survival_days_positive, event_type_factor) ~ origin_commit_category + repo
    } else {
      Surv(survival_days_positive, event_type_factor) ~ origin_commit_category
    }
    coxFormula <- Surv(fgstart, fgstop, fgstatus) ~ origin_commit_category + strata(repo)
  } else if (modelName == "file_role") {
    fgFormula <- if (fineGrayExpansion == "global") {
      Surv(survival_days_positive, event_type_factor) ~ origin_commit_category + file_primary_role + repo
    } else {
      Surv(survival_days_positive, event_type_factor) ~ origin_commit_category + file_primary_role
    }
    coxFormula <- Surv(fgstart, fgstop, fgstatus) ~ origin_commit_category + file_primary_role + strata(repo)
  } else {
    stop(paste("unknown model:", modelName))
  }

  fineGrayStartTime <- Sys.time()
  if (fineGrayExpansion == "global") {
    logMessage("Running global Fine-Gray expansion: event_type=", eventType, " model=", modelName)
    expanded <- expandAndCompressFineGrayGlobal(
      data = data,
      eventType = eventType,
      modelName = modelName,
      fgFormula = fgFormula
    )
  } else {
    logMessage("Running Fine-Gray expansion by repo: event_type=", eventType, " model=", modelName)
    expanded <- expandAndCompressFineGrayByRepo(
      data = data,
      eventType = eventType,
      modelName = modelName,
      fgFormula = fgFormula
    )
  }
  fgData <- expanded$data
  logMessage(
    "Finished Fine-Gray expansion: event_type=", eventType,
    " model=", modelName,
    " mode=", fineGrayExpansion,
    " raw_fg_rows=", format(expanded$raw_fg_rows, big.mark = ","),
    " compressed_fg_rows=", format(expanded$compressed_fg_rows, big.mark = ","),
    " repo_errors=", length(expanded$repo_errors),
    " elapsed=", formatElapsed(fineGrayStartTime)
  )
  rawFgRows <- expanded$raw_fg_rows
  sanitizedFgRows <- expanded$sanitized_fg_rows
  compressedFgRows <- expanded$compressed_fg_rows
  droppedFgRows <- expanded$dropped_fg_rows
  skippedNoTargetRepos <- expanded$skipped_no_target_repos
  skippedNoTargetRows <- expanded$skipped_no_target_rows
  if (!is.null(expanded$global_error) && expanded$global_error != "") {
    return(list(
      status = "skipped",
      reason = paste("finegray_error:", expanded$global_error),
      fg_rows = 0,
      raw_fg_rows = rawFgRows,
      dropped_fg_rows = droppedFgRows,
      sanitized_fg_rows = sanitizedFgRows,
      compressed_fg_rows = compressedFgRows,
      skipped_no_target_repos = skippedNoTargetRepos,
      skipped_no_target_rows = skippedNoTargetRows,
      finegray_expansion = fineGrayExpansion
    ))
  }
  if (length(expanded$repo_errors) > 0) {
    logMessage(
      "Fine-Gray repo expansion errors: event_type=", eventType,
      " model=", modelName,
      " errors=", paste(head(expanded$repo_errors, 5), collapse = " | "),
      ifelse(length(expanded$repo_errors) > 5, " | ...", "")
    )
  }
  if (droppedFgRows > 0) {
    logMessage(
      "Dropped invalid Fine-Gray interval rows: event_type=", eventType,
      " model=", modelName,
      " dropped=", format(droppedFgRows, big.mark = ","),
      " raw_fg_rows=", format(rawFgRows, big.mark = ",")
    )
  }
  if (nrow(fgData) == 0) {
    return(list(
      status = "skipped",
      reason = "no_valid_finegray_intervals",
      fg_rows = 0,
      raw_fg_rows = rawFgRows,
      dropped_fg_rows = droppedFgRows,
      sanitized_fg_rows = sanitizedFgRows,
      compressed_fg_rows = compressedFgRows,
      skipped_no_target_repos = skippedNoTargetRepos,
      skipped_no_target_rows = skippedNoTargetRows,
      finegray_expansion = fineGrayExpansion
    ))
  }
  if (sum(fgData$fgstatus, na.rm = TRUE) <= 0) {
    return(list(
      status = "skipped",
      reason = "no_finegray_events_after_interval_filter",
      fg_rows = nrow(fgData),
      raw_fg_rows = rawFgRows,
      dropped_fg_rows = droppedFgRows,
      sanitized_fg_rows = sanitizedFgRows,
      compressed_fg_rows = compressedFgRows,
      skipped_no_target_repos = skippedNoTargetRepos,
      skipped_no_target_rows = skippedNoTargetRows,
      finegray_expansion = fineGrayExpansion
    ))
  }
  coxStartTime <- Sys.time()
  logMessage(
    "Running Cox model fit on Fine-Gray data: event_type=", eventType,
    " model=", modelName,
    " fg_rows=", format(nrow(fgData), big.mark = ",")
  )
  fit <- tryCatch(
    coxph(
      coxFormula,
      data = fgData,
      weights = fgwt,
      cluster = repo,
      control = coxph.control(timefix = FALSE) 
      # why are we doing timefix is FALSE? the reason is that some intervals can be extremely short, 
      # especially for same-day events after we convert zero-day survival into a tiny positive value. 
      # By default, coxph() uses timefix = TRUE, which tries to detect near-tied floating-point times and collapse/adjust them. 
      #That can turn a tiny positive interval into an effectively zero-length interval, triggering the aeqSurv error.
    ),
    error = function(err) err
  )
  logMessage(
    "Finished Cox model fit on Fine-Gray data: event_type=", eventType,
    " model=", modelName,
    " elapsed=", formatElapsed(coxStartTime)
  )
  if (inherits(fit, "error")) {
    return(list(
      status = "skipped",
      reason = paste("coxph_error:", fit$message),
      fg_rows = nrow(fgData),
      raw_fg_rows = rawFgRows,
      dropped_fg_rows = droppedFgRows,
      sanitized_fg_rows = sanitizedFgRows,
      compressed_fg_rows = compressedFgRows,
      skipped_no_target_repos = skippedNoTargetRepos,
      skipped_no_target_rows = skippedNoTargetRows,
      finegray_expansion = fineGrayExpansion
    ))
  }

  list(
    status = "fit",
    fit = fit,
    fg_rows = nrow(fgData),
    raw_fg_rows = rawFgRows,
    dropped_fg_rows = droppedFgRows,
    sanitized_fg_rows = sanitizedFgRows,
    compressed_fg_rows = compressedFgRows,
    skipped_no_target_repos = skippedNoTargetRepos,
    skipped_no_target_rows = skippedNoTargetRows,
    finegray_expansion = fineGrayExpansion
  )
}

extractFineGraySummary <- function(eventType, modelName, fitResult, data) {
  totalLines <- sum(data$weight)
  eventLines <- eventCount(data, eventType)
  censoredLines <- sum(data$weight[data$event_observed == 0])
  competingLines <- sum(data$weight[data$event_observed == 1]) - eventLines
  roleWeightedCounts <- tapply(data$weight, data$origin_commit_category, sum)

  if (fitResult$status != "fit") {
    return(data.frame(
      event_type = eventType,
      event_taxonomy = eventTaxonomy,
      model = modelName,
      status = fitResult$status,
      reason = fitResult$reason,
      finegray_expansion = ifelse(is.null(fitResult$finegray_expansion), fineGrayExpansion, fitResult$finegray_expansion),
      n_rows = nrow(data),
      fg_rows = ifelse(is.null(fitResult$fg_rows), NA, fitResult$fg_rows),
      raw_fg_rows = ifelse(is.null(fitResult$raw_fg_rows), NA, fitResult$raw_fg_rows),
      sanitized_fg_rows = ifelse(is.null(fitResult$sanitized_fg_rows), NA, fitResult$sanitized_fg_rows),
      compressed_fg_rows = ifelse(is.null(fitResult$compressed_fg_rows), NA, fitResult$compressed_fg_rows),
      dropped_fg_rows = ifelse(is.null(fitResult$dropped_fg_rows), NA, fitResult$dropped_fg_rows),
      skipped_no_target_repos = ifelse(is.null(fitResult$skipped_no_target_repos), NA, fitResult$skipped_no_target_repos),
      skipped_no_target_rows = ifelse(is.null(fitResult$skipped_no_target_rows), NA, fitResult$skipped_no_target_rows),
      weighted_lines = totalLines,
      event_lines = eventLines,
      competing_event_lines = competingLines,
      censored_lines = censoredLines,
      repo_clusters = length(unique(data$repo)),
      human_lines = unname(roleWeightedCounts[["not_ai_coauthored"]]),
      ai_lines = unname(roleWeightedCounts[["ai_coauthored"]]),
      log_subhazard = NA_real_,
      subhazard_ratio = NA_real_,
      ci_lower = NA_real_,
      ci_upper = NA_real_,
      robust_se = NA_real_,
      wald_p_value = NA_real_,
      rds_path = NA_character_,
      rds_error = NA_character_,
      stringsAsFactors = FALSE
    ))
  }

  fitSummary <- summary(fitResult$fit)
  coefficientTable <- fitSummary$coefficients
  confidenceIntervalTable <- fitSummary$conf.int
  coefficientRow <- "origin_commit_categoryai_coauthored"
  if (!(coefficientRow %in% rownames(coefficientTable))) {
    stop(paste("missing coefficient row:", coefficientRow))
  }

  data.frame(
    event_type = eventType,
    event_taxonomy = eventTaxonomy,
    model = modelName,
    status = "fit",
    reason = "",
    finegray_expansion = fitResult$finegray_expansion,
    n_rows = nrow(data),
    fg_rows = fitResult$fg_rows,
    raw_fg_rows = fitResult$raw_fg_rows,
    sanitized_fg_rows = fitResult$sanitized_fg_rows,
    compressed_fg_rows = fitResult$compressed_fg_rows,
    dropped_fg_rows = fitResult$dropped_fg_rows,
    skipped_no_target_repos = fitResult$skipped_no_target_repos,
    skipped_no_target_rows = fitResult$skipped_no_target_rows,
    weighted_lines = totalLines,
    event_lines = eventLines,
    competing_event_lines = competingLines,
    censored_lines = censoredLines,
    repo_clusters = length(unique(data$repo)),
    human_lines = unname(roleWeightedCounts[["not_ai_coauthored"]]),
    ai_lines = unname(roleWeightedCounts[["ai_coauthored"]]),
    log_subhazard = as.numeric(coefficientTable[coefficientRow, "coef"]),
    subhazard_ratio = as.numeric(coefficientTable[coefficientRow, "exp(coef)"]),
    ci_lower = as.numeric(confidenceIntervalTable[coefficientRow, "lower .95"]),
    ci_upper = as.numeric(confidenceIntervalTable[coefficientRow, "upper .95"]),
    robust_se = as.numeric(coefficientTable[coefficientRow, "robust se"]),
    wald_p_value = as.numeric(coefficientTable[coefficientRow, "Pr(>|z|)"]),
    rds_path = ifelse(is.null(fitResult$rds_path), NA_character_, fitResult$rds_path),
    rds_error = ifelse(is.null(fitResult$rds_error), NA_character_, fitResult$rds_error),
    stringsAsFactors = FALSE
  )
}

printNativeFineGraySummary <- function(eventType, modelName, fitResult) {
  cat("\n")
  cat("## ", eventType, " / ", modelName, "\n", sep = "")
  if (fitResult$status != "fit") {
    cat("status: skipped\n")
    cat("reason: ", fitResult$reason, "\n", sep = "")
    return(invisible(NULL))
  }
  cat("fg_rows: ", format(fitResult$fg_rows, big.mark = ","), "\n", sep = "")
  print(summary(fitResult$fit))
  invisible(NULL)
}


## Run analysis

eventTypes <- eventTypes[eventTypes %in% levels(line_lifecycle$event_type_factor)]
if (length(eventTypes) == 0) {
  stop("none of the requested event types are present in the input data")
}
logMessage("Event types present and selected: ", paste(eventTypes, collapse = ", "))
for (eventType in eventTypes) {
  logMessage(
    "  event_type=", eventType,
    " weighted_events=",
    format(round(eventCount(line_lifecycle, eventType)), big.mark = ",")
  )
}

cifAndPlotStartTime <- Sys.time()
logMessage("Starting CIF estimation and plotting...")
cifCurves <- buildCifCurveCache(line_lifecycle, eventTypes)
cifEstimates <- buildCifEstimates(cifCurves, eventTypes, timePoints)
cifEstimatesPath <- file.path(outputDir, paste0(outputStem, "_cif_estimates.csv"))
write.csv(cifEstimates, cifEstimatesPath, row.names = FALSE)
logMessage("Wrote CIF estimates: ", cifEstimatesPath)

cifPlotPaths <- c()
for (eventType in eventTypes) {
  plotStartTime <- Sys.time()
  plotPath <- file.path(outputDir, paste0(outputStem, "_cif_", eventType, ".pdf"))
  logMessage("Plotting CIF: ", eventType)
  if (writeCifPlot(cifCurves, eventType, plotPath, maxPlotDays, timePoints)) {
    cifPlotPaths <- c(cifPlotPaths, plotPath)
  }
  logStageDone(paste0("CIF plot event_type=", eventType), plotStartTime)
}
combinedPlotStartTime <- Sys.time()
combinedPlotPath <- file.path(outputDir, paste0(outputStem, "_cif_combined.pdf"))
logMessage("Plotting combined CIF overlay")
if (writeCombinedCifPlot(cifCurves, eventTypes, combinedPlotPath, maxPlotDays, timePoints)) {
  cifPlotPaths <- c(cifPlotPaths, combinedPlotPath)
}
logStageDone("Combined CIF overlay plot", combinedPlotStartTime)
logStageDone("CIF estimation and plotting", cifAndPlotStartTime)

modelSummaryPath <- file.path(outputDir, paste0(outputStem, "_model_summary.csv"))
nativeSummaryPath <- file.path(outputDir, paste0(outputStem, "_native_summaries.txt"))

if (skipFineGrayModels) {
  logMessage("Skipping Fine-Gray model fitting because --cif-only was set.")
  modelSummaryPath <- "skipped_cif_only"
  nativeSummaryPath <- "skipped_cif_only"
} else {
  modelRows <- list()
  nativeSummaryOutput <- c()
  rowIndex <- 1
  for (eventType in eventTypes) {
    for (modelName in modelNames) {
      modelStartTime <- Sys.time()
      logMessage("Fitting Fine-Gray model: event_type=", eventType, " model=", modelName)
      fitResult <- fitFineGrayModel(line_lifecycle, eventType, modelName)
      fitResult <- saveFittedFineGrayModel(fitResult, eventType, modelName)
      if (fitResult$status == "fit") {
        logMessage(
          "Finished Fine-Gray model: event_type=", eventType,
          " model=", modelName,
          " status=", fitResult$status,
          " elapsed=", formatElapsed(modelStartTime)
        )
      } else {
        logMessage(
          "Finished Fine-Gray model: event_type=", eventType,
          " model=", modelName,
          " status=", fitResult$status,
          " reason=", fitResult$reason,
          " elapsed=", formatElapsed(modelStartTime)
        )
      }
      nativeSummaryOutput <- c(
        nativeSummaryOutput,
        capture.output(printNativeFineGraySummary(eventType, modelName, fitResult))
      )
      modelRows[[rowIndex]] <- extractFineGraySummary(eventType, modelName, fitResult, line_lifecycle)
      rowIndex <- rowIndex + 1
    }
  }

  modelSummary <- do.call(rbind, modelRows)
  write.csv(modelSummary, modelSummaryPath, row.names = FALSE)
  logMessage("Wrote model summary CSV: ", modelSummaryPath)
  writeLines(nativeSummaryOutput, nativeSummaryPath)
  logMessage("Wrote native summary TXT: ", nativeSummaryPath)

  cat(paste(nativeSummaryOutput, collapse = "\n"))
  cat("\n")
}

cat("\nfine_gray_rq1:\n")
cat("  input_file: ", inputFile, "\n", sep = "")
cat("  rows: ", format(nrow(line_lifecycle), big.mark = ","), "\n", sep = "")
cat("  weighted_lines: ", format(round(sum(line_lifecycle$weight)), big.mark = ","), "\n", sep = "")
cat("  repos: ", length(unique(line_lifecycle$repo)), "\n", sep = "")
cat("  event_taxonomy: ", eventTaxonomy, "\n", sep = "")
cat("  event_types: ", paste(eventTypes, collapse = ", "), "\n", sep = "")
cat("  finegray_expansion: ", fineGrayExpansion, "\n", sep = "")
cat("  model_names: ", paste(modelNames, collapse = ", "), "\n", sep = "")
cat("  save_rds_models: ", saveRdsModels, "\n", sep = "")
if (saveRdsModels && !skipFineGrayModels) {
  cat("  rds_dir: ", rdsDir, "\n", sep = "")
}
cat("  cif_only: ", skipFineGrayModels, "\n", sep = "")
cat("  wrote_model_summary_csv: ", modelSummaryPath, "\n", sep = "")
cat("  wrote_native_summary_txt: ", nativeSummaryPath, "\n", sep = "")
cat("  wrote_cif_estimates_csv: ", cifEstimatesPath, "\n", sep = "")
for (plotPath in cifPlotPaths) {
  cat("  wrote_cif_plot_pdf: ", plotPath, "\n", sep = "")
}
