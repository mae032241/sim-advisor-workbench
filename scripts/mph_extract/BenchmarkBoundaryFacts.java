/* Read geometric entity extents only; no geometry, mesh or solver execution. */
import com.comsol.model.*;
import com.comsol.model.util.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.lang.reflect.*;

public final class BenchmarkBoundaryFacts {
  public static void main(String[] a) throws Exception {
    try {
      ModelUtil.initStandalone(false);ModelUtil.showProgress(false);
      Model m=ModelUtil.loadCopy("GeometryFacts",a[0]);GeomSequence g=m.component("comp1").geom("geom1");
      Object measure=g.getClass().getMethod("measureFinal").invoke(g);
      Object selection=measure.getClass().getMethod("selection").invoke(measure);
      selection.getClass().getMethod("geom",int.class).invoke(selection,a.length>3&&a[3].equals("domains")?g.getSDim():g.getSDim()-1);
      List<Object> facts=new ArrayList<>();
      for(String raw:a[2].split(",")) {
        int id=Integer.parseInt(raw);
        selection.getClass().getMethod("set",int[].class).invoke(selection,(Object)new int[]{id});
        Object bbox=measure.getClass().getMethod("getBoundingBox").invoke(measure);
        facts.add(BenchmarkGeometry.map("entity_id",id,"bounding_box_native_axis_pairs",bbox));
      }
      Files.write(Paths.get(a[1]),BenchmarkGeometry.json(BenchmarkGeometry.map("length_unit",g.lengthUnit(),"dimension",g.getSDim(),"boundary_facts",facts,"mesh_run",false,"solver_run",false)).getBytes(StandardCharsets.UTF_8));
      ModelUtil.remove("GeometryFacts");ModelUtil.clear();System.exit(0);
    } catch(Throwable e) {e.printStackTrace();System.exit(2);}
  }
}
