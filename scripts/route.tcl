# route.tcl — NanoRoute detailed routing
setNanoRouteMode -routeWithTimingDriven true
setNanoRouteMode -routeWithSiDriven true
# Requires a clock tree; aborts if clock topology is unconstrained.
routeDesign -globalDetail
verifyConnectivity -type all -error 0
