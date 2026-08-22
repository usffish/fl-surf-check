"""
Curated list of Florida surf spots.

Each spot records:
    name          - common name of the break
    region        - rough geographic region, for grouping in output
    lat, lon      - approximate coordinates of the takeoff area / beach access
    facing_deg    - compass bearing (0-360) the beach faces, i.e. the direction
                    swells/waves generally arrive FROM. This is used to work out
                    which wind directions are "offshore" (facing_deg + 180) vs
                    "onshore" (facing_deg) for that spot.
    tide_station  - nearest NOAA CO-OPS tide prediction station ID
    notes         - short human-readable context

IMPORTANT CAVEAT: facing_deg values are reasonable approximations based on the
general orientation of the Florida coastline at each spot, not surveyed
data. The coastline curves, so treat these as "close enough for offshore/
onshore classification," not survey-grade bearings. Feel free to tune them
if you find a spot's wind call is consistently backwards.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Spot:
    name: str
    region: str
    lat: float
    lon: float
    facing_deg: float  # direction the beach faces (where swell comes from)
    tide_station: str  # NOAA CO-OPS station ID
    notes: str = ""
    tz: str = "America/New_York"  # IANA timezone for this spot (Panhandle Gulf spots are Central)

    @property
    def offshore_deg(self) -> float:
        """Wind direction that blows straight offshore (best case) for this spot."""
        return (self.facing_deg + 180) % 360


# NOAA CO-OPS tide prediction stations used below, all verified against the
# live station list rather than assumed:
#   8720030 Fernandina Beach, Amelia River
#   8720218 Mayport (Bar Pilot Dock)
#   8721604 Port Canaveral (Trident Pier)
#   8722670 Lake Worth Pier (ocean)
#   8723214 Virginia Key, Biscayne Bay
#   8726034 Siesta Key, Big Sarasota Pass
#   8726520 St. Petersburg
#   8726724 Clearwater Beach
#   8729210 Panama City Beach
#   8729840 Pensacola
#
# That verification caught a real error: 8728690, previously used for Panama
# City Beach and commented as such, is actually APALACHICOLA - roughly 100
# miles east. Those tides were wrong.

SPOTS = [
    # --- Northeast Florida ---
    Spot("Fernandina Beach", "Northeast FL", 30.6700, -81.4370, 100, "8720030",
         "Exposed beach break near the GA line, catches NE windswell well."),
    Spot("Jacksonville Beach Pier", "Northeast FL", 30.2825, -81.3862, 95, "8720218",
         "Consistent pier break, popular and crowded on good days."),
    Spot("Ponte Vedra Beach", "Northeast FL", 30.2394, -81.3823, 95, "8720218",
         "Quieter beach break, similar exposure to Jax Beach."),
    Spot("St. Augustine Pier", "Northeast FL", 29.8896, -81.2870, 92, "8720218",
         "Reliable pier peak, works on most swell/wind combos."),
    Spot("Vilano Beach", "Northeast FL", 29.9169, -81.2971, 92, "8720218",
         "Beach break just north of the St. Augustine inlet."),

    # --- East Central Florida (Space Coast) ---
    Spot("Flagler Beach Pier", "East Central FL", 29.4747, -81.1256, 90, "8721604",
         "Sand-bottom pier peak, holds size well."),
    Spot("Ormond Beach", "East Central FL", 29.2858, -81.0284, 90, "8721604",
         "Wide, flat beach break, good for longer boards."),
    Spot("New Smyrna Beach Inlet", "East Central FL", 29.0439, -80.9231, 88, "8721604",
         "One of the most consistent and famous spots in FL; can get crowded/sharky."),
    Spot("Ponce Inlet", "East Central FL", 29.0844, -80.9214, 88, "8721604",
         "Jetty break next to New Smyrna, similar exposure."),
    Spot("Playalinda Beach", "East Central FL", 28.6192, -80.6828, 87, "8721604",
         "Inside Canaveral National Seashore, less crowded, no rentals nearby."),
    Spot("Cocoa Beach Pier", "East Central FL", 28.3200, -80.6076, 87, "8721604",
         "Classic longboard-friendly beach break, very consistent."),
    Spot("Patrick Space Force Base / Satellite Beach", "East Central FL", 28.1761, -80.5967, 86, "8721604",
         "Good sandbars, slightly less crowded than Cocoa."),
    Spot("Indialantic / 3rd Ave Melbourne Beach", "East Central FL", 28.0836, -80.5601, 86, "8721604",
         "Well-known local peak, works on a wide range of swells."),
    Spot("Sebastian Inlet (First Peak)", "East Central FL", 27.8595, -80.4479, 85, "8721604",
         "One of the best and most famous point/inlet breaks on the east coast."),

    # --- Treasure Coast / Palm Beaches ---
    Spot("Vero Beach", "Treasure Coast", 27.6386, -80.3576, 85, "8721604",
         "Beach break, decent on E/NE swells."),
    Spot("Fort Pierce Inlet", "Treasure Coast", 27.4736, -80.2848, 84, "8722670",
         "Jetty break, can be punchy near the inlet."),
    Spot("Stuart / House of Refuge", "Treasure Coast", 27.1959, -80.1652, 83, "8722670",
         "Rocky-bottom break, works well on smaller/cleaner days."),
    Spot("Juno Beach / Jupiter", "Treasure Coast", 26.8770, -80.0531, 82, "8722670",
         "Consistent beach break, several access points."),
    Spot("Palm Beach (Reef Road)", "Palm Beaches", 26.7153, -80.0364, 82, "8722670",
         "Well-known local reef/beach break near the inlet."),
    Spot("Boynton Beach Inlet", "Palm Beaches", 26.5423, -80.0575, 81, "8722670",
         "Jetty peak, good shape when the swell direction lines up."),

    # --- Broward / Miami-Dade ---
    Spot("Deerfield Beach", "Broward", 26.3184, -80.0808, 80, "8722670",
         "Beach break, generally smaller/softer than spots further north."),
    Spot("Fort Lauderdale (Sunrise/Sebastian St)", "Broward", 26.1417, -80.1064, 79, "8723214",
         "Popular urban beach break."),
    Spot("South Beach / South Pointe, Miami", "Miami-Dade", 25.7702, -80.1330, 78, "8723214",
         "Jetty break at the south end of Miami Beach; needs real swell to work."),

    # --- Gulf Coast (generally smaller/weaker, wind/tropical-system driven) ---
    Spot("Panama City Beach (Pier Park)", "Gulf Coast (Panhandle)", 30.1766, -85.8055, 175, "8729210",
         "Gulf beach break; best during cold-front or tropical-system swells.",
         tz="America/Chicago"),
    Spot("Navarre Beach", "Gulf Coast (Panhandle)", 30.3877, -86.8631, 170, "8729840",
         "Gulf beach break, similar wind/swell dependence as Panama City.",
         tz="America/Chicago"),
    Spot("Pensacola Beach", "Gulf Coast (Panhandle)", 30.3280, -87.1669, 165, "8729840",
         "Gulf beach break near the pass; needs a strong weather system to fire.",
         tz="America/Chicago"),

    # --- Added from the historical record (see README, "How many spots") ---
    # Ranked by the share of days each supplies a good session over the 2021-10
    # to 2026-08 record, and by how often each would be the top recommendation
    # from a range of Florida origins.

    # East coast gaps: the highest-quality water in the state.
    Spot("Jupiter Inlet", "Treasure Coast", 26.9450, -80.0730, 82, "8722670",
         "Best in the state on the record: a good session on 35.8% of days."),
    Spot("Hobe Sound", "Treasure Coast", 27.0600, -80.1100, 82, "8722670",
         "Quiet stretch just north of Jupiter; 30.5% good days."),
    Spot("Lake Worth Pier", "Palm Beaches", 26.6120, -80.0350, 81, "8722670",
         "Consistent pier peak, 27.6% good days."),
    Spot("Delray Beach", "Palm Beaches", 26.4600, -80.0600, 81, "8722670",
         "Beach break with easy access; 26.3% good days."),
    Spot("Apollo Beach (Canaveral NS)", "East Central FL", 28.8700, -80.8100, 87, "8721604",
         "North end of Canaveral National Seashore. Sits in a better forecast "
         "cell than New Smyrna inlet and wins more often than any other spot."),
    Spot("Spessard Holland", "East Central FL", 28.0330, -80.5400, 86, "8721604",
         "South Melbourne Beach park; 24.3% good days."),
    Spot("Daytona Beach Shores", "East Central FL", 29.1800, -80.9800, 89, "8721604",
         "Fills the gap between Ormond and Ponce Inlet."),

    # --- Gulf Coast (west central) ---
    # Small and fickle - a good session on only 1-5% of days - but 30-60
    # minutes from Tampa Bay rather than three hours. Simulated from a Tampa
    # zip these win roughly a third of all days purely on drive time, which is
    # exactly the trade the value score exists to make.
    Spot("Honeymoon Island", "Gulf Coast (West Central)", 28.0660, -82.8320, 265, "8726724",
         "Best of the Tampa-area Gulf beaches; wins 21% of days from Tampa."),
    Spot("Clearwater Beach", "Gulf Coast (West Central)", 27.9775, -82.8271, 265, "8726724",
         "Most accessible Tampa-area break; needs a strong west wind or a system."),
    Spot("Indian Rocks Beach", "Gulf Coast (West Central)", 27.8870, -82.8480, 265, "8726724",
         "Quieter than Clearwater, same swell window."),
    Spot("Treasure Island", "Gulf Coast (West Central)", 27.7692, -82.7690, 260, "8726724",
         "Wide beach south of Clearwater."),
    Spot("St. Pete Beach", "Gulf Coast (West Central)", 27.7253, -82.7412, 258, "8726520",
         "Southern end of the Pinellas barrier islands."),
    Spot("Lido Key", "Gulf Coast (West Central)", 27.3100, -82.5750, 250, "8726034",
         "Sarasota's town beach; occasional shape on a west swell."),
    Spot("Siesta Key", "Gulf Coast (West Central)", 27.2676, -82.5540, 250, "8726034",
         "Famously flat, but works on a strong system."),
    Spot("Venice Beach", "Gulf Coast (West Central)", 27.1000, -82.4540, 248, "8726034",
         "Southern Sarasota county; slightly more exposed than Siesta."),
]
