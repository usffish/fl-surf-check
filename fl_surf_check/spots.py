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


# NOAA CO-OPS tide stations used above, for reference:
#   8720030 Fernandina Beach
#   8720218 Mayport (Bar Pilots Dock), FL
#   8721604 Trident Pier, Port Canaveral, FL
#   8722670 Lake Worth Pier, FL
#   8723214 Virginia Key, Biscayne Bay, FL
#   8724580 Key West, FL
#   8726520 St. Petersburg, FL
#   8728690 Panama City, FL
#   8729840 Pensacola, FL

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
    Spot("Panama City Beach (Pier Park)", "Gulf Coast (Panhandle)", 30.1766, -85.8055, 175, "8728690",
         "Gulf beach break; best during cold-front or tropical-system swells.",
         tz="America/Chicago"),
    Spot("Navarre Beach", "Gulf Coast (Panhandle)", 30.3877, -86.8631, 170, "8729840",
         "Gulf beach break, similar wind/swell dependence as Panama City.",
         tz="America/Chicago"),
    Spot("Pensacola Beach", "Gulf Coast (Panhandle)", 30.3280, -87.1669, 165, "8729840",
         "Gulf beach break near the pass; needs a strong weather system to fire.",
         tz="America/Chicago"),
]
