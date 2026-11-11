/* Geometry preparation only: no mesh/physics/study/solver execution. */
import com.comsol.model.*;
import com.comsol.model.util.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.lang.reflect.Array;
import java.util.*;

public final class PrepareGeometry {
  static Map<String,Object> map(Object... a){Map<String,Object> r=new LinkedHashMap<>();for(int i=0;i<a.length;i+=2)r.put((String)a[i],a[i+1]);return r;}
  static String quote(String s){StringBuilder r=new StringBuilder("\"");for(char c:s.toCharArray()){if(c=='"'||c=='\\')r.append('\\').append(c);else if(c<32)r.append(String.format("\\u%04x",(int)c));else r.append(c);}return r.append('"').toString();}
  static String json(Object v){if(v==null)return "null";if(v instanceof Number||v instanceof Boolean)return v.toString();if(v instanceof Map){StringJoiner j=new StringJoiner(",","{","}");for(Object x:((Map<?,?>)v).entrySet()){Map.Entry<?,?> e=(Map.Entry<?,?>)x;j.add(quote(e.getKey().toString())+":"+json(e.getValue()));}return j.toString();}if(v.getClass().isArray()){StringJoiner j=new StringJoiner(",","[","]");for(int i=0;i<Array.getLength(v);i++)j.add(json(Array.get(v,i)));return j.toString();}return quote(v.toString());}
  static Map<String,Object> stats(GeomSequence g){return map("unit",g.lengthUnit(),"domains",g.getNDomains(),"boundaries",g.getNBoundaries(),"edges",g.getNEdges(),"vertices",g.getNVertices(),"bounding_box_native_order",g.getBoundingBox(),"is_assembly",g.isAssembly(),"kernel",g.geomRep());}
  static Model fresh(String filename,String unit) {
    Model model=ModelUtil.create("NeutralGeometry");model.label("geometry_only.mph");model.author("");model.comments("");
    model.component().create("comp1",true);model.component("comp1").label("Part 01");
    model.component("comp1").geom().create("geom1",3);GeomSequence g=model.component("comp1").geom("geom1");g.label("Geometry");g.lengthUnit(unit);
    g.feature("fin").set("action","assembly");g.feature("fin").set("createpairs",false);
    g.create("imp1","Import");g.feature("imp1").label("Part 01");g.feature("imp1").set("filename",filename);
    g.run();return model;
  }
  public static void main(String[] args){
    try{
      String mode=args[0];Path input=Paths.get(args[1]),out=Paths.get(args[2]),report=Paths.get(args[3]);Files.createDirectories(out);
      ModelUtil.initStandalone(false);ModelUtil.showProgress(false);
      Map<String,Object> result=map("mode",mode,"runtime",ModelUtil.getComsolVersion(),"mesh_run",false,"solver_run",false,"physical_validation","not_run");
      if(mode.equals("fresh")) {
        Model model=fresh(input.toString(),"mm");GeomSequence g=model.component("comp1").geom("geom1");
        result.put("fresh_geometry",stats(g));result.put("created_from_empty_model",true);result.put("source_model_loaded",false);
        result.put("physics_tags",model.component("comp1").physics().tags());result.put("material_tags",model.component("comp1").material().tags());
        result.put("mesh_tags",model.component("comp1").mesh().tags());result.put("study_tags",model.study().tags());result.put("solver_tags",model.sol().tags());
        result.put("multiphysics_tags",model.component("comp1").multiphysics().tags());result.put("pair_tags",model.component("comp1").pair().tags());
        model.save(out.resolve("geometry_only.mph").toString());result.put("geometry_build",true);
      } else {
        Model model=ModelUtil.loadCopy("PrivateSource",input.toString());String component=model.component().tags()[0];String tag=model.component(component).geom().tags()[0];
        GeomSequence g=model.component(component).geom(tag);result.put("stored_geometry",stats(g));result.put("geometry_build",false);
        if(mode.equals("export")) {
          g.run();result.put("geometry_build",true);result.put("built_geometry",stats(g));
          g.export().setType("nativebin");g.exportFinal(out.resolve("native_geometry.mphbin").toString());
          Model neutral=fresh(out.resolve("native_geometry.mphbin").toString(),g.lengthUnit());GeomSequence ng=neutral.component("comp1").geom("geom1");
          try{
            ng.export().setType("step");ng.export().setLengthUnitSTEP("mm");ng.export().selection().set(ng.objectNames());ng.export(out.resolve("intermediate.step").toString());
            result.put("step_export",true);result.put("step_unit","mm");
          }catch(Exception first){
            try{ng.geomRep("cadps");ng.run();ng.export().setType("step");ng.export().setLengthUnitSTEP("mm");ng.export().selection().set(ng.objectNames());ng.export(out.resolve("intermediate.step").toString());result.put("step_export",true);result.put("step_unit","mm");result.put("cad_kernel_conversion",true);}
            catch(Exception second){result.put("step_export",false);result.put("step_export_error",second.getMessage());}
          }
          result.put("neutral_geometry",stats(ng));
        }
      }
      Files.write(report,json(result).getBytes(StandardCharsets.UTF_8));System.out.println("GEOMETRY_PREPARATION_OK");System.exit(0);
    }catch(Throwable error){error.printStackTrace();System.exit(2);}
  }
}
